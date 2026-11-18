"""P35: credentials are replaced before message text reaches the ledger.

Every credential-shaped value below is assembled at runtime from short parts, so no line of
this file holds a literal a secret scanner would flag. All values are synthetic.
"""
from dataclasses import replace
import io
import json
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from slack_dispatch import service
from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.config.schema import REDACTION_KINDS, Redaction
from slack_dispatch.inbox import cli
from slack_dispatch.ingest.redact import KINDS, is_marker, redact, redact_counts, redact_json
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline, redaction_settings
from slack_dispatch.router.classifier import NullSuggester
from tests.test_pipeline_integration import SAMPLE, event, fixture_config

STAMP = "2026-01-01T00:00:00+00:00"
OWNER = "U0000000093"  # always_human.owner_user of the sample config


def aws_id():
    return "AK" + "IA" + "Z" * 4 + "7" * 4 + "Q" * 8


def aws_pair_value():
    return "wJ" + "aL" * 15 + "/K7MD+EN"  # 40 characters


def slack_value(kind="b"):
    return "xo" + "x" + kind + "-" + "1" * 12 + "-" + "a" * 12


def app_value():
    return "xa" + "pp-" + "1-" + "A0" * 6 + "-" + "9" * 6


def github_value(prefix="gh" + "p_"):
    return prefix + "A1" * 18


def provider_value():
    return "s" + "k-" + "proj_" + "Z9" * 12


def bearer_value():
    return "ey" + "J0" * 10 + "." + "x1" * 8


def pem_block(end=True):
    kind = "RSA " + "PRIV" + "ATE KEY"
    head = "-" * 5 + "BEGIN " + kind + "-" * 5
    tail = "-" * 5 + "END " + kind + "-" * 5
    body = "\n".join(["MIIE" + "q" * 60] * 3)
    return head + "\n" + body + ("\n" + tail if end else "")


def word_value():
    return "hunt" + "er" + "2" * 6


def google_value():
    return "AI" + "za" + "Sy" + "B1c2" * 8 + "x"  # 35 characters after the prefix


def stripe_value(prefix="sk" + "_live_"):
    return prefix + "Ab12" * 6


def webhook_path():
    return "T0000AAAA/B0000BBBB/" + "x1" * 12


def jwt_value():
    return "ey" + "JhbGciOi" + "." + "ey" + "JzdWIiOi" + "." + "s1Gn" * 4


def basic_value():
    return "dXNlcj" + "pwYXNz" + "d29yZDEy"


def session_value():
    return "IQoJb3JpZ2lu" + "A1b2/C3d4+" * 30


def hex_commit():
    return "0123456789abcdef" * 2 + "01234567"  # 40 hex characters


def placeholder(kind):
    return "[redacted:" + kind + "]"


class RedactFunctionTests(unittest.TestCase):
    def check(self, text, expected, kinds):
        out, found = redact(text)
        self.assertEqual(out, expected)
        self.assertEqual(found, kinds)
        again, more = redact(out)
        self.assertEqual((again, more), (out, ()))  # idempotent

    def test_kinds_are_the_config_kinds(self):
        self.assertEqual(KINDS, REDACTION_KINDS)
        self.assertEqual(len(set(KINDS)), 14)

    def test_aws_access_key_id_prefixes_and_word_bounds(self):
        for prefix in ("AK" + "IA", "AS" + "IA", "AI" + "DA", "AR" + "OA", "AG" + "PA"):
            value = prefix + "B" * 8 + "2" * 8
            self.check("key " + value + ".", "key " + placeholder("aws_access_key_id") + ".",
                       ("aws_access_key_id",))
        longer = aws_id() + "Z"
        self.check("id " + longer, "id " + longer, ())
        self.check("x" + aws_id(), "x" + aws_id(), ())
        self.check(aws_id()[:-1], aws_id()[:-1], ())

    def test_aws_secret_needs_the_key_id_or_a_secret_label_on_the_line(self):
        pair = aws_pair_value()
        self.check("value " + pair, "value " + pair, ())
        self.check("id " + aws_id() + "\nvalue " + pair,
                   "id " + placeholder("aws_access_key_id") + "\nvalue "
                   + placeholder("aws_secret_access_key"),
                   ("aws_access_key_id", "aws_secret_access_key"))
        self.check("Secret access key is " + pair,
                   "Secret access key is " + placeholder("aws_secret_access_key"),
                   ("aws_secret_access_key",))
        self.check("the secret\n" + pair, "the secret\n" + pair, ())
        self.check(pair + " is not a secret", pair + " is not a secret", ())

    def test_aws_secret_kind_alone_still_uses_the_key_id_rule(self):
        text = "id " + aws_id() + " value " + aws_pair_value()
        out, found = redact(text, ("aws_secret_access_key",))
        self.assertEqual(out, "id " + aws_id() + " value " + placeholder("aws_secret_access_key"))
        self.assertEqual(found, ("aws_secret_access_key",))
        self.assertEqual(redact(out, ("aws_secret_access_key",)), (out, ()))

    def test_slack_tokens(self):
        for value in (slack_value("b"), slack_value("p"), slack_value("a"), slack_value("r"),
                      slack_value("s"), app_value()):
            self.check("use " + value + " now", "use " + placeholder("slack_token") + " now",
                       ("slack_token",))
        short = "xo" + "xb-" + "12345"
        self.check(short, short, ())

    def test_github_tokens(self):
        for prefix in ("gh" + "p_", "gh" + "o_", "gh" + "s_", "gh" + "u_", "github" + "_pat_"):
            self.check("tok " + github_value(prefix), "tok " + placeholder("github_token"),
                       ("github_token",))
        short = "gh" + "p_" + "A" * 10
        self.check(short, short, ())

    def test_api_key_prefix_and_left_boundary(self):
        self.check("key " + provider_value(), "key " + placeholder("api_key"), ("api_key",))
        prose = "risk" + "-assessment-framework-document"  # "sk-" inside a word
        self.check(prose, prose, ())
        short = "s" + "k-" + "abc"
        self.check(short, short, ())

    def test_bearer_token_keeps_the_scheme_word(self):
        self.check("Authorization: Bearer " + bearer_value(),
                   "Authorization: Bearer " + placeholder("bearer_token"), ("bearer_token",))
        prose = "Bearer responsibilities include reviews"
        self.check(prose, prose, ())

    def test_private_key_with_and_without_end_line(self):
        self.check("before\n" + pem_block() + "\nafter",
                   "before\n" + placeholder("private_key") + "\nafter", ("private_key",))
        self.check("before\n" + pem_block(end=False) + "\ntrailing lines",
                   "before\n" + placeholder("private_key"), ("private_key",))
        two = pem_block() + "\nmiddle\n" + pem_block()
        out, found = redact(two)
        self.assertEqual(out, placeholder("private_key") + "\nmiddle\n" + placeholder("private_key"))
        self.assertEqual(redact_counts(two)[1]["private_key"], 2)

    def test_labelled_values(self):
        value = word_value()
        for label in ("password", "Password", "PASSWD", "secret", "api key", "API_KEY", "api-key",
                      "access key", "Access_Key", "token", "비밀번호"):
            for separator in (": ", "=", " = ", ":"):
                text = "my " + label + separator + value + " thanks"
                self.check(text, "my " + label + separator + placeholder("labelled_secret") + " thanks",
                           ("labelled_secret",))
        self.check('{"password": "' + value + '"}',
                   '{"password": "' + placeholder("labelled_secret") + '"}', ("labelled_secret",))
        self.check("password: short1", "password: short1", ())
        self.check("password:\n" + value, "password:\n" + value, ())
        self.check("password reset is done", "password reset is done", ())
        self.check("tokenizer: " + value, "tokenizer: " + value, ())

    def test_url_password_only_the_password_is_replaced(self):
        url = "https://admin:" + word_value() + "@db.example.com/path"
        self.check("see " + url, "see https://admin:" + placeholder("url_password")
                   + "@db.example.com/path", ("url_password",))
        self.check("<" + url + "|db>", "<https://admin:" + placeholder("url_password")
                   + "@db.example.com/path|db>", ("url_password",))

    def test_ordinary_text_is_never_altered(self):
        samples = [
            "hi <@U0000000001> and <@U0000000002|name>, see <#C0000000002|general>",
            "slack:C0000000002:1000.000001 and thread 1700000000.000100",
            "commit " + hex_commit() + " fixed it",
            "sha256 " + "ab" * 32,
            "antidisestablishmentarianism " * 3,
            "https://example.com/a/b?c=d&e=f and <https://example.com:8080/x|link>",
            "mailto:someone@example.com",
            "Token count was high today",
            "The API key rotation is scheduled",
            "",
        ]
        for text in samples:
            self.check(text, text, ())

    def test_non_string_values_pass_through(self):
        self.assertEqual(redact(None), (None, ()))
        self.assertEqual(redact_counts(5)[0], 5)

    def test_kinds_restriction(self):
        text = slack_value() + " " + github_value()
        out, found = redact(text, ("github_token",))
        self.assertEqual(out, slack_value() + " " + placeholder("github_token"))
        self.assertEqual(found, ("github_token",))

    def test_idempotence_over_a_mixed_message(self):
        text = "\n".join([
            "id " + aws_id(), "value " + aws_pair_value(), "token: " + slack_value(),
            "repo " + github_value(), "maps " + google_value(), "pay " + stripe_value(),
            "hook https://hooks." + "slack.com/services/" + webhook_path(), "jwt " + jwt_value(),
            "Authorization: Basic " + basic_value(),
            "api_key=" + provider_value(), "Bearer " + bearer_value(),
            "https://u:" + word_value() + "@h.example.com", "secret: " + word_value(),
            pem_block(), "password = '" + word_value() + "'", "<@U0000000093> please check"])
        once, kinds = redact(text)
        self.assertEqual(set(kinds), set(KINDS))
        self.assertEqual(redact(once), (once, ()))
        for part in (aws_id(), aws_pair_value(), slack_value(), provider_value(), bearer_value(),
                     word_value(), github_value(), google_value(), stripe_value(), webhook_path(),
                     jwt_value(), basic_value(), "MIIE"):
            self.assertNotIn(part, once)
        self.assertIn("<@U0000000093> please check", once)

    def test_redact_json_redacts_every_string_value(self):
        value = {"event": {"text": "token: " + slack_value(), "blocks": [{"text": word_value()},
                                                                        "secret=" + word_value()]},
                 "n": 3, slack_value(): "key stays"}
        clean, counts = redact_json(value)
        self.assertEqual(clean["event"]["text"], "token: " + placeholder("slack_token"))
        self.assertEqual(clean["event"]["blocks"][0]["text"], word_value())
        self.assertEqual(clean["event"]["blocks"][1], "secret=" + placeholder("labelled_secret"))
        self.assertEqual(clean["n"], 3)
        self.assertEqual(counts, {"slack_token": 1, "labelled_secret": 1})

    def test_linear_time_on_large_adversarial_input(self):
        size = 200_000
        pieces = ["a" * 997, "token" + " " * 400, "https://" + "u" * 300, "Bearer " + "x" * 80,
                  "secret " + "B" * 40, "-" * 500, "/+=" * 200, "s" + "k-" * 100,
                  "token: " + "<" * 300, "sessionToken" * 20, "eyJ" + ".eyJ" * 50,
                  "hooks.slack.com/services/" * 4, "Authorization: Basic " * 4, "AIza" * 30,
                  "*password*:" * 10, "password:\u00a0" * 10,
                  "-----BEGIN " + "A" * 60, "password:" * 20, "AK" + "IA" * 50]
        text = ""
        while len(text) < size:
            text += " ".join(pieces) + "\n"
        for case in (text[:size], "a" * size, "-" * size, ("token" + " " * 1000) * 200,
                     "a://" + "b" * size, ("Bearer " + "y" * 50 + " ") * 3500):
            started = time.perf_counter()
            redact(case)
            self.assertLess(time.perf_counter() - started, 2.0)


class Fix1RedactTests(unittest.TestCase):
    """Fix pass: verifier findings 1 to 6."""

    def check(self, text, expected, kinds):
        out, found = redact(text)
        self.assertEqual(out, expected)
        self.assertEqual(found, kinds)
        self.assertEqual(redact(out), (out, ()))  # idempotent

    def same(self, text):
        self.check(text, text, ())

    def test_finding1_mentions_and_links_are_never_part_of_a_value(self):
        mention = "<@" + OWNER + ">"
        self.same("token: " + mention + " hi")
        self.same("secret=<#C0000000002|general>")
        self.same("api key: <https://example.com/docs|docs>")
        self.same("password: <!here> please")
        self.check("password: abcd" + "1234efgh " + mention,
                   "password: " + placeholder("labelled_secret") + " " + mention,
                   ("labelled_secret",))
        self.check("token=" + word_value() + mention,
                   "token=" + placeholder("labelled_secret") + mention, ("labelled_secret",))
        self.check("token=" + word_value() + "<#C0000000002>",
                   "token=" + placeholder("labelled_secret") + "<#C0000000002>",
                   ("labelled_secret",))
        self.check(mention + " key:\n" + pem_block() + "\n" + mention,
                   mention + " key:\n" + placeholder("private_key") + "\n" + mention,
                   ("private_key",))

    def test_finding2_identifier_tail_labels(self):
        value = word_value()
        for label in ("SessionToken", "sessionToken", "accessToken", "refreshToken",
                      "clientSecret", "SECRET_KEY", "secret_key", "AWS_SECRET_ACCESS_KEY",
                      "AWS_SESSION_TOKEN", "passphrase", "private_key", "myApiKey", "GITHUBTOKEN",
                      "x-api-key", "DB_PASSWORD", "비밀번호", "비번", "암호", "패스워드"):
            with self.subTest(label=label):
                self.check(label + "=" + value, label + "=" + placeholder("labelled_secret"),
                           ("labelled_secret",))
        self.check('{"password": "' + value + '"}',
                   '{"password": "' + placeholder("labelled_secret") + '"}', ("labelled_secret",))
        self.check('"SessionToken": "' + session_value() + '",',
                   '"SessionToken": "' + placeholder("labelled_secret") + '",', ("labelled_secret",))

    def test_finding2_sts_json_paste_is_fully_redacted(self):
        session_id = "AS" + "IA" + "Q" * 8 + "7" * 8
        text = ('{\n  "Credentials": {\n    "AccessKeyId": "' + session_id + '",\n'
                '    "SecretAccessKey": "' + aws_pair_value() + '",\n'
                '    "SessionToken": "' + session_value() + '",\n'
                '    "Expiration": "2026-01-01T00:00:00Z"\n  }\n}')
        out, kinds = redact(text)
        for part in (session_id, aws_pair_value(), session_value()):
            self.assertNotIn(part, out)
        self.assertIn('"Expiration": "2026-01-01T00:00:00Z"', out)
        self.assertEqual(kinds, ("aws_access_key_id", "aws_secret_access_key", "labelled_secret"))
        self.assertEqual(redact(out), (out, ()))

    def test_finding2_prose_and_partial_words_do_not_match(self):
        for text in ("the password is " + word_value(), "Session token expired yesterday",
                     "xtoken: " + word_value(), "tokenizer: " + word_value(),
                     "Secrets: none today", "암호화: 완료", "password:", "passwords are rotated",
                     "accessTokens: " + word_value()):
            with self.subTest(text=text):
                self.same(text)

    def test_finding3_emphasis_backticks_and_unicode_spaces(self):
        value = word_value()
        mark = placeholder("labelled_secret")
        cases = {
            "*password:* " + value: "*password:* " + mark,
            "*password*: " + value: "*password*: " + mark,
            "_token_ = " + value: "_token_ = " + mark,
            "~secret~: " + value: "~secret~: " + mark,
            "`password`: `" + value + "`": "`password`: `" + mark + "`",
            "password: *" + value + "*": "password: *" + mark + "*",
            "password:`" + value + "`": "password:`" + mark + "`",
            "password:\u00a0" + value: "password:\u00a0" + mark,
            "password: \u2003" + value: "password: \u2003" + mark,
            "password=\u202f" + value: "password=\u202f" + mark,
        }
        for text, expected in cases.items():
            with self.subTest(text=text.replace(value, "<value>")):
                self.check(text, expected, ("labelled_secret",))

    def test_finding4_only_an_exact_marker_is_skipped(self):
        mark = placeholder("labelled_secret")
        self.check("password: " + mark + word_value(), "password: " + mark, ("labelled_secret",))
        self.check("password: " + word_value() + mark, "password: " + mark, ("labelled_secret",))
        self.check("password: [redacted:not_a_kind]", "password: " + mark, ("labelled_secret",))
        self.same("password: " + mark)
        self.same("password: '" + mark + "',")
        url_mark = placeholder("url_password")
        self.check("https://u:" + url_mark + word_value() + "@h.example.com/",
                   "https://u:" + url_mark + "@h.example.com/", ("url_password",))
        self.same("https://u:" + url_mark + "@h.example.com/")
        self.assertTrue(is_marker(mark))
        self.assertFalse(is_marker(mark + "x"))
        self.assertFalse(is_marker("[redacted:not_a_kind]"))

    def test_finding5_bearer_word_in_any_case_and_digit_rule(self):
        for word in ("Bearer", "bearer", "BEARER", "BeArEr"):
            self.check(word + " " + bearer_value(), word + " " + placeholder("bearer_token"),
                       ("bearer_token",))
        self.same("BEARER RESPONSIBILITIESX")
        self.same("bearer " + "abcdefghijklmnopq")

    def test_finding6_google_api_key(self):
        self.check("maps " + google_value(), "maps " + placeholder("google_api_key"),
                   ("google_api_key",))
        self.same("AI" + "za" + "short")
        self.same("AI" + "za" + "A" * 36)
        self.same("xAI" + "za" + "Sy" + "B1c2" * 8 + "x")

    def test_finding6_stripe_keys(self):
        for prefix in ("sk" + "_live_", "rk" + "_live_", "sk" + "_test_"):
            self.check("pay " + stripe_value(prefix), "pay " + placeholder("stripe_key"),
                       ("stripe_key",))
        self.same("sk" + "_live_" + "abc")
        self.same("pk" + "_live_" + "Ab12" * 6)

    def test_finding6_slack_refresh_and_client_tokens(self):
        for kind in ("e", "c"):
            self.check("use " + slack_value(kind), "use " + placeholder("slack_token"),
                       ("slack_token",))
        self.same("xo" + "xe-" + "123")

    def test_finding6_slack_webhook_keeps_the_host(self):
        prefix = "https://hooks." + "slack.com/services/"
        self.check("post to " + prefix + webhook_path() + " now",
                   "post to " + prefix + placeholder("slack_webhook") + " now", ("slack_webhook",))
        self.check("<" + prefix + webhook_path() + ">",
                   "<" + prefix + placeholder("slack_webhook") + ">", ("slack_webhook",))
        self.same(prefix + "T0")
        self.same("https://slack.com/services/" + webhook_path())

    def test_finding6_jwt(self):
        self.check("token " + jwt_value() + " ok", "token " + placeholder("jwt") + " ok", ("jwt",))
        self.check("Bearer " + jwt_value(), "Bearer " + placeholder("bearer_token"),
                   ("bearer_token",))
        self.same("ey" + "Jabcdefg.notajwt")
        self.same("ey" + "JhbGciOi" + "." + "ey" + "JzdWIiOi")
        self.same("ey" + "JhbGciOi" + "." + "abcdefgh" + "." + "s1Gn" * 4)

    def test_finding6_basic_auth(self):
        self.check("-H 'Authorization: Basic " + basic_value() + "'",
                   "-H 'Authorization: Basic " + placeholder("basic_auth") + "'", ("basic_auth",))
        self.check("authorization:basic " + basic_value() + "==",
                   "authorization:basic " + placeholder("basic_auth"), ("basic_auth",))
        self.same("Authorization: Basic abc")
        self.same("Basic training starts " + basic_value()[:10])

    def test_new_kinds_are_valid_config_kinds(self):
        import yaml
        raw = yaml.safe_load(Path(SAMPLE).read_text(encoding="utf-8"))
        raw["redaction"] = {"kinds": ["google_api_key", "stripe_key", "slack_webhook", "jwt",
                                      "basic_auth"]}
        self.assertEqual(parse_config(raw).redaction.kinds,
                         ("google_api_key", "stripe_key", "slack_webhook", "jwt", "basic_auth"))


class RedactionConfigTests(unittest.TestCase):
    def raw(self):
        import yaml
        return yaml.safe_load(Path(SAMPLE).read_text(encoding="utf-8"))

    def test_default_is_enabled_with_every_kind(self):
        config = load_config(SAMPLE)
        self.assertEqual(config.redaction, Redaction(True, REDACTION_KINDS))
        self.assertEqual(redaction_settings(Mock(spec=[])), Redaction())

    def test_enabled_and_kinds(self):
        raw = self.raw()
        raw["redaction"] = {"enabled": False, "kinds": ["slack_token", "private_key", "slack_token"]}
        config = parse_config(raw)
        self.assertEqual(config.redaction, Redaction(False, ("slack_token", "private_key")))
        raw["redaction"] = None
        self.assertEqual(parse_config(raw).redaction, Redaction())

    def test_invalid_values_raise_clear_errors(self):
        cases = [({"enabled": "yes"}, "redaction.enabled must be a boolean"),
                 ({"kinds": "slack_token"}, "redaction.kinds must be a non-empty list"),
                 ({"kinds": []}, "redaction.kinds must be a non-empty list"),
                 ({"kinds": ["password"]}, "redaction.kinds entries must be one of"),
                 ({"kinds": [3]}, "redaction.kinds entries must be one of"),
                 ({"enable": True}, "redaction keys must be enabled or kinds"),
                 (["slack_token"], "redaction must be a mapping")]
        for value, message in cases:
            raw = self.raw()
            raw["redaction"] = value
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                parse_config(raw)
            self.assertIn(message, str(caught.exception))


class PipelineRedactionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-redact-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        base = fixture_config(self.root)
        self.config = replace(base, ledger=replace(base.ledger, store_body=True))
        self.store = LedgerStore(self.config.ledger.path, store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id, session_pointer=lane.session, paused=lane.paused)
        self.now = 1000.0
        self.lane, self.human = Mock(), Mock()

    def pipeline(self, config=None):
        config = config or self.config
        notifiers = [FileInboxNotifier(config.inbox.root, self.store.get_message), self.lane, self.human]
        clock = lambda: self.now  # noqa: E731
        return Pipeline(config, self.store, NullSuggester(), notifiers,
                        WakePolicyEngine(config.wake_policy, clock), clock)

    def body(self, mid):
        return self.store.get_message(mid, include_body=True)["body"]

    def disabled(self):
        return replace(self.config, redaction=Redaction(enabled=False))

    def test_new_message_stores_only_the_redacted_text_and_logs_once(self):
        pipeline = self.pipeline()
        text = "[support] here: " + slack_value() + " and " + github_value()
        with self.assertLogs("slack_dispatch", level="INFO") as logs:
            result = pipeline.handle_event(event(text=text))
        self.assertEqual(self.body(result.message_id),
                         "[support] here: " + placeholder("slack_token") + " and "
                         + placeholder("github_token"))
        lines = [line for line in logs.output if "Redacted credentials" in line]
        self.assertEqual(lines, ["INFO:slack_dispatch.pipeline:Redacted credentials id="
                                 + result.message_id + " kinds=slack_token,github_token"])
        joined = "\n".join(logs.output)
        self.assertNotIn(slack_value(), joined)
        self.assertNotIn(github_value(), joined)
        self.assertEqual(pipeline.snapshot()["credentials_redacted"], 1)
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        inbox = (self.root / "inbox/support.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(slack_value(), inbox)

    def test_duplicate_delivery_is_not_logged_or_counted_again(self):
        pipeline = self.pipeline()
        text = "[support] " + slack_value()
        pipeline.handle_event(event(text=text))
        with self.assertLogs("slack_dispatch", level="INFO") as logs:
            pipeline.handle_event(event(text=text))  # same event id
            pipeline.handle_event(event(9, ts="1000.000001", text=text))  # same message id
            logging.getLogger("slack_dispatch").info("marker")  # assertLogs needs one record
        self.assertFalse([line for line in logs.output if "Redacted" in line])
        self.assertEqual(pipeline.snapshot()["credentials_redacted"], 1)

    def test_clean_text_has_no_counter_and_no_log(self):
        pipeline = self.pipeline()
        result = pipeline.handle_event(event())
        self.assertEqual(self.body(result.message_id), "[support] private fixture text")
        self.assertNotIn("credentials_redacted", pipeline.snapshot())

    def test_disabled_redaction_stores_the_original(self):
        pipeline = self.pipeline(self.disabled())
        text = "[support] " + slack_value()
        result = pipeline.handle_event(event(text=text))
        self.assertEqual(self.body(result.message_id), text)
        self.assertNotIn("credentials_redacted", pipeline.snapshot())
        self.assertFalse(self.store.redaction.enabled)

    def test_kinds_restrict_what_the_pipeline_redacts(self):
        config = replace(self.config, redaction=Redaction(kinds=("github_token",)))
        pipeline = self.pipeline(config)
        text = "[support] " + slack_value() + " " + github_value()
        result = pipeline.handle_event(event(text=text))
        self.assertEqual(self.body(result.message_id),
                         "[support] " + slack_value() + " " + placeholder("github_token"))

    def test_edit_stores_only_the_redacted_text(self):
        pipeline = self.pipeline()
        first = pipeline.handle_event(event())
        edited = "[support] now with password: " + word_value()
        with self.assertLogs("slack_dispatch", level="INFO") as logs:
            pipeline.handle_event(event(2, ts="1000.000001", subtype="message_changed", text=edited))
        self.assertEqual(self.body(first.message_id),
                         "[support] now with password: " + placeholder("labelled_secret"))
        self.assertIn("Redacted credentials id=" + first.message_id + " kinds=labelled_secret",
                      "\n".join(logs.output))
        self.assertNotIn(word_value(), "\n".join(logs.output))

    def test_edit_with_redaction_disabled_stores_the_original(self):
        pipeline = self.pipeline(self.disabled())
        first = pipeline.handle_event(event())
        edited = "[support] now with password: " + word_value()
        pipeline.handle_event(event(2, ts="1000.000001", subtype="message_changed", text=edited))
        self.assertEqual(self.body(first.message_id), edited)

    def test_unchanged_edit_of_a_body_stored_before_redaction_does_not_wake(self):
        text = "[support] " + slack_value()
        self.store.redaction = Redaction(enabled=False)
        self.store.record_event(event(text=text))  # stored before P35, in plain text
        mid = "slack:C0000000002:1000.000001"
        self.store.set_route(mid, "support", "explicit_tag", state="acked")
        pipeline = self.pipeline()
        result = pipeline.handle_event(event(2, ts="1000.000001", subtype="message_changed", text=text))
        self.assertEqual(pipeline.edits_ignored, 1)
        self.assertEqual(result.state, "acked")
        self.assertEqual(self.body(mid), "[support] " + placeholder("slack_token"))

    def test_thread_context_root_is_redacted(self):
        pipeline = self.pipeline()
        root = event(5, ts="1000.000005", text="root with " + bearer_value() + " Bearer "
                     + bearer_value())
        with self.assertLogs("slack_dispatch", level="INFO") as logs:
            result = pipeline.record_context(root)
        self.assertTrue(result.created)
        self.assertEqual(self.body(result.message_id),
                         "root with " + bearer_value() + " Bearer " + placeholder("bearer_token"))
        self.assertIn("kinds=bearer_token", "\n".join(logs.output))

    def test_thread_context_with_redaction_disabled_keeps_the_original(self):
        pipeline = self.pipeline(self.disabled())
        text = "root " + slack_value()
        result = pipeline.record_context(event(5, ts="1000.000005", text=text))
        self.assertEqual(self.body(result.message_id), text)

    def test_owner_mention_survives_redaction(self):
        pipeline = self.pipeline()
        text = "<@" + OWNER + "> the value is " + slack_value()
        result = pipeline.handle_event(event(text=text))
        row = self.store.get_message(result.message_id, include_body=True)
        self.assertEqual(row["attention"], "owner_mention")
        self.assertEqual(row["body"], "<@" + OWNER + "> the value is " + placeholder("slack_token"))

    def test_fix1_mention_after_a_label_still_raises_owner_mention(self):
        pipeline = self.pipeline()
        text = "[support] token: <@" + OWNER + "> hi"
        result = pipeline.handle_event(event(text=text))
        row = self.store.get_message(result.message_id, include_body=True)
        self.assertEqual(row["body"], text)
        self.assertEqual(row["attention"], "owner_mention")
        self.assertNotIn("credentials_redacted", pipeline.snapshot())

    def test_routing_and_wake_text_is_the_redacted_text(self):
        seen = []

        class Spy(WakePolicyEngine):
            def offer(self, lane, message, author=None):
                seen.append(message.get("text"))
                return super().offer(lane, message, author)

        notifiers = [FileInboxNotifier(self.config.inbox.root, self.store.get_message), self.lane,
                     self.human]
        clock = lambda: self.now  # noqa: E731
        pipeline = Pipeline(self.config, self.store, NullSuggester(), notifiers,
                            Spy(self.config.wake_policy, clock), clock)
        result = pipeline.handle_event(event(text="[support] " + slack_value()))
        self.assertEqual(result.lane, "support")  # the explicit tag still routes
        self.assertEqual(seen, ["[support] " + placeholder("slack_token")])

    def test_store_body_off_still_counts_and_stores_nothing(self):
        store = LedgerStore(str(self.root / "plain.sqlite3"))
        store.open()
        store.migrate()
        self.addCleanup(store.close)
        for lane in self.config.lanes:
            store.upsert_lane(lane.id)
        config = replace(self.config, ledger=replace(self.config.ledger, store_body=False))
        clock = lambda: self.now  # noqa: E731
        pipeline = Pipeline(config, store, NullSuggester(), [Mock(), Mock(), Mock()],
                            WakePolicyEngine(config.wake_policy, clock), clock)
        result = pipeline.handle_event(event(text="[support] " + slack_value()))
        self.assertIsNone(store.get_message(result.message_id, include_body=True)["body"])
        self.assertEqual(pipeline.snapshot()["credentials_redacted"], 1)


class StoreRedactionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-redact-store-")
        self.addCleanup(temp.cleanup)
        self.store = LedgerStore(str(Path(temp.name) / "ledger.sqlite3"), store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)

    def record(self, text, number=1):
        return self.store.record_event(dict(
            source="slack", event_id=f"EvDUMMY{number}", event_type="message", channel="CDUMMY",
            ts=f"100.{number:06d}", user="UDUMMY", received_at=STAMP, text=text))

    def test_record_event_redacts_by_default_and_logs_without_values(self):
        with self.assertLogs("slack_dispatch.ledger.store", level="INFO") as logs:
            result = self.record("x " + app_value())
        self.assertEqual(self.store.get_message(result.message_id, include_body=True)["body"],
                         "x " + placeholder("slack_token"))
        self.assertEqual(logs.output, ["INFO:slack_dispatch.ledger.store:Redacted credentials "
                                       "id=slack:CDUMMY:100.000001 kinds=slack_token"])
        self.assertEqual(self.store.credentials_redacted, 1)

    def test_set_body_redacts_and_disabled_store_keeps_the_original(self):
        self.record(None, 1)
        mid = "slack:CDUMMY:100.000001"
        self.assertTrue(self.store.set_body(mid, "k " + provider_value()))
        self.assertEqual(self.store.get_message(mid, include_body=True)["body"],
                         "k " + placeholder("api_key"))
        self.store.redaction = Redaction(enabled=False)
        self.record(None, 2)
        other = "slack:CDUMMY:100.000002"
        self.assertTrue(self.store.set_body(other, "k " + provider_value()))
        self.assertEqual(self.store.get_message(other, include_body=True)["body"],
                         "k " + provider_value())
        self.assertEqual(self.store.credentials_redacted, 1)


class FakeHistory:
    """Answers bounded history reads from a ts -> message map."""

    def __init__(self, messages):
        self.messages = messages

    def api_call(self, method, http_verb=None, params=None):
        found = self.messages.get(params["latest"])
        return {"ok": True, "messages": [found] if found else []}


class RehydrateRedactionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-redact-rehydrate-")
        self.addCleanup(temp.cleanup)
        base = fixture_config(Path(temp.name))
        self.config = replace(base, ledger=replace(base.ledger, store_body=True),
                              sources=load_config(SAMPLE).sources)
        self.now = 2_000_000.0
        self.ts = f"{self.now - 60:.6f}"
        store = LedgerStore(self.config.ledger.path, store_body=True)
        store.open()
        store.migrate()
        store.record_event(dict(source="slack", event_type="message", event_id="EvDUMMY1",
                                channel="CDUMMY", ts=self.ts, user="UDUMMY", received_at=STAMP))
        store.close()
        self.mid = f"slack:CDUMMY:{self.ts}"
        self.text = "reply with token: " + slack_value()

    def body(self):
        store = LedgerStore(self.config.ledger.path)
        store.open()
        try:
            return store.get_message(self.mid, include_body=True)["body"]
        finally:
            store.close()

    def test_rehydrated_body_is_redacted(self):
        client = FakeHistory({self.ts: dict(ts=self.ts, text=self.text)})
        out = io.StringIO()
        with self.assertLogs("slack_dispatch", level="INFO") as logs:
            code = service.run_rehydrate(self.config, 7, client=client, clock=lambda: self.now,
                                         sleep=lambda seconds: None, stdout=out)
        self.assertEqual(code, 0)
        self.assertEqual(self.body(), "reply with token: " + placeholder("slack_token"))
        joined = out.getvalue() + "\n".join(logs.output)
        self.assertIn("Redacted credentials id=" + self.mid + " kinds=slack_token", joined)
        self.assertNotIn(slack_value(), joined)

    def run_rehydrate(self, config):
        client = FakeHistory({self.ts: dict(ts=self.ts, text=self.text)})
        out = io.StringIO()
        code = service.run_rehydrate(config, 7, client=client, clock=lambda: self.now,
                                     sleep=lambda seconds: None, stdout=out)
        return code, out.getvalue()

    def test_rehydrate_honours_redaction_enabled_from_the_config(self):
        config = replace(self.config, redaction=Redaction(enabled=True))
        code, out = self.run_rehydrate(config)
        self.assertEqual(code, 0)
        self.assertIn("filled=1", out)
        self.assertEqual(self.body(), "reply with token: " + placeholder("slack_token"))

    def test_rehydrate_with_redaction_disabled_stores_the_original(self):
        config = replace(self.config, redaction=Redaction(enabled=False))
        code, out = self.run_rehydrate(config)
        self.assertEqual(code, 0)
        self.assertIn("filled=1", out)
        self.assertEqual(self.body(), self.text)

    def test_rehydrate_honours_redaction_kinds(self):
        config = replace(self.config, redaction=Redaction(kinds=("github_token",)))
        code, _ = self.run_rehydrate(config)
        self.assertEqual(code, 0)
        self.assertEqual(self.body(), self.text)


class RepairRedactTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-redact-repair-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = str(self.root / "ledger.sqlite3")
        self.now = 3_000_000.0
        store = LedgerStore(self.path, store_body=True)
        store.redaction = Redaction(enabled=False)  # rows written before P35
        store.open()
        store.migrate()
        self.rows = {}
        for number, (age, text) in enumerate([
                (60, "plain " + slack_value()),
                (120, "id " + aws_id() + " value " + aws_pair_value()),
                (40 * 86400, "old " + github_value()),
                (30, "nothing to see here"),
                (90, None)], start=1):
            ts = f"{self.now - age:.6f}"
            store.record_event(dict(source="slack", event_type="message", event_id=f"EvDUMMY{number}",
                                    channel="CDUMMY", ts=ts, user="UDUMMY", received_at=STAMP,
                                    text=text))
            self.rows[number] = f"slack:CDUMMY:{ts}"
        payload = json.dumps({"event": {"text": "password: " + word_value(), "n": 1}})
        with store._transaction() as db:
            db.execute("INSERT INTO events_raw (event_id, message_id, received_at, payload) "
                       "VALUES (?, ?, ?, ?)", ("EvRAW1", None, "1970-01-01T00:00:00+00:00", payload))
            db.execute("INSERT INTO events_raw (event_id, message_id, received_at, payload) "
                       "VALUES (?, ?, ?, ?)", ("EvRAW2", None, "unreadable", "token=" + word_value()))
        store.close()
        self.store = LedgerStore(self.path)
        self.store.open()
        self.addCleanup(self.store.close)
        clock = patch.object(cli, "REDACT_CLOCK", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["repair", "redact", *args], store=self.store, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def body(self, number):
        return self.store.get_message(self.rows[number], include_body=True)["body"]

    def raw(self, event_id):
        return self.store._rows("SELECT payload FROM events_raw WHERE event_id=?", (event_id,))[0]["payload"]

    def secrets(self):
        return (slack_value(), aws_id(), aws_pair_value(), github_value(), word_value())

    def test_dry_run_reports_counts_without_values_and_changes_nothing(self):
        before = [self.body(number) for number in range(1, 6)]
        code, out, err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = out.splitlines()
        # EvRAW1 is outside the window by its received_at; EvRAW2 has no readable stamp.
        self.assertEqual(lines[0], "repair redact mode=dry-run days=30 scanned=4 rows=3")
        self.assertIn(self.rows[1] + " slack_token", lines)
        self.assertIn(self.rows[2] + " aws_access_key_id,aws_secret_access_key", lines)
        self.assertIn("events_raw:EvRAW2 labelled_secret", lines)
        self.assertIn("kind=slack_token rows=1 matches=1", lines)
        self.assertIn("kind=labelled_secret rows=1 matches=1", lines)
        self.assertEqual(lines[-1], "dry run: nothing changed; rerun with --apply")
        for value in self.secrets():
            self.assertNotIn(value, out)
        self.assertNotIn("plain", out)
        self.assertEqual([self.body(number) for number in range(1, 6)], before)

    def test_apply_rewrites_matches_in_place(self):
        code, out, _ = self.run_cli("--apply", "--days", "60")
        self.assertEqual(code, 0)
        self.assertIn("updated=5", out.splitlines())
        self.assertEqual(self.body(1), "plain " + placeholder("slack_token"))
        self.assertEqual(self.body(2), "id " + placeholder("aws_access_key_id") + " value "
                         + placeholder("aws_secret_access_key"))
        self.assertEqual(self.body(3), "old " + placeholder("github_token"))
        self.assertEqual(self.body(4), "nothing to see here")
        self.assertIsNone(self.body(5))
        self.assertEqual(json.loads(self.raw("EvRAW1")),
                         {"event": {"text": "password: " + placeholder("labelled_secret"), "n": 1}})
        self.assertEqual(self.raw("EvRAW2"), "token=" + placeholder("labelled_secret"))
        code, out, _ = self.run_cli("--days", "60")
        self.assertIn("rows=0", out.splitlines()[0])
        for value in self.secrets():
            self.assertNotIn(value, out)

    def test_days_window_and_json_output(self):
        code, out, _ = self.run_cli("--days", "1", "--json")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual((result["mode"], result["days"], result["scanned"]), ("dry-run", 1, 4))
        self.assertEqual([row["id"] for row in result["rows"]],
                         sorted([self.rows[1], self.rows[2]]) + ["EvRAW2"])
        self.assertEqual(result["counts"]["aws_access_key_id"], {"rows": 1, "matches": 1})
        for value in self.secrets():
            self.assertNotIn(value, out)

    def test_config_kinds_limit_the_repair(self):
        config_path = self.root / "config.yaml"
        import yaml
        raw = yaml.safe_load(Path(SAMPLE).read_text(encoding="utf-8"))
        raw["redaction"] = {"enabled": False, "kinds": ["github_token"]}
        config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        code, out, _ = self.run_cli("--apply", "--days", "60", "--config", str(config_path))
        self.assertEqual(code, 0)
        self.assertEqual(self.body(3), "old " + placeholder("github_token"))
        self.assertEqual(self.body(1), "plain " + slack_value())

    def test_invalid_days_is_rejected(self):
        for days in ("0", "-3", "x"):
            code, out, err = self.run_cli("--days", days)
            self.assertEqual(code, 2)
            self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
