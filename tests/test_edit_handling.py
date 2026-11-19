"""P37 edit handling: cosmetic edits, edits that add an owner mention, edit size hint.

Offline only: the real SQLite ledger in a temporary directory, the file inbox there, mock
lane and human notifiers and a fixed clock. Dummy ids and neutral sample names only.
"""
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import yaml

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.console import actions
from slack_dispatch.inbox import cli
from slack_dispatch.inbox.files import append, read, record
from slack_dispatch.ingest.membership import NOT_MEMBER
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import (
    Pipeline, canonical_token, cosmetic_edit, cosmetic_tokens, edit_delta, edit_parts,
    followup_ignore_cosmetic,
)
from slack_dispatch.router.classifier import NullSuggester
from slack_dispatch.router.engine import NO_MENTION
from tests.test_pipeline_integration import SAMPLE, event, fixture_config

TAGGED = "C0000000002"      # default null: the [support] tag routes to support
NORMAL = "C0000000001"      # default support
MENTION_ONLY = "C0000000004"  # default owner, mention_only: true in the sample
EXCLUDED = "C0000000003"    # excluded_channels in the sample
DEFAULT_LANE = "owner"
OWNER = "U0000000093"       # always_human.owner_user in the sample
MENTION_USER = "U0000000092"
PLAIN = "U0000000088"
OTHER_USER = "U0000000077"
NAME = "Operator Kim"
ROOT_TS = "1000.000001"
BASE = "[support] please check the dummy report"
LINK = "https://example.invalid/dummy"
RECORD_KEYS = {"id", "delivered_at", "channel", "ts", "thread_ts", "followup", "kind"}
POLICIES = ("changed_after_ack", "always", "never")


def edit(number, text, **changes):
    """A message_changed event of an existing message (a new event id, the same ts)."""
    changes.setdefault("ts", ROOT_TS)
    return event(number, subtype="message_changed", text=text, **changes)


class Case(unittest.TestCase):
    """One isolated instance per `build` call (temporary ledger, inbox and state)."""

    def build(self, *, policy="changed_after_ack", cosmetic=False, owner_mode=False,
              names=(), store_body=True):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p37-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        base = fixture_config(self.root)
        config = replace(base, wake_policy=replace(
            base.wake_policy, followup=policy, followup_ignore_cosmetic=cosmetic),
            always_human=replace(base.always_human, mention_texts=tuple(names)))
        if owner_mode:
            config = replace(config, mention_only_owner_channels=(MENTION_ONLY,))
        self.config = config
        self.store = LedgerStore(config.ledger.path, store_body=store_body)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in config.lanes:
            self.store.upsert_lane(lane.id, session_pointer=lane.session, paused=lane.paused)
        self.now = 1000.0
        clock = lambda: self.now  # noqa: E731
        self.lane, self.human = Mock(), Mock()
        engine = WakePolicyEngine(config.wake_policy, clock)
        self.offers = Mock(side_effect=engine.offer)
        engine.offer = self.offers
        self.pipeline = Pipeline(config, self.store, NullSuggester(),
                                 [FileInboxNotifier(config.inbox.root, self.store.get_message),
                                  self.lane, self.human], engine, clock)
        return self.pipeline

    def records(self, lane="support"):
        path = Path(self.config.inbox.root) / f"{lane}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def all_records(self):
        return [row for path in sorted(Path(self.config.inbox.root).glob("*.jsonl"))
                for row in self.records(path.stem)]

    def row(self, mid):
        return self.store.get_message(mid, include_body=True)

    def notices(self, mid):
        return [row["kind"] for row in
                self.store._rows("SELECT kind FROM human_notices WHERE message_id=?", (mid,))]

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["--inbox-root", self.config.inbox.root, *argv], store=self.store,
                        stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue()


class CosmeticEditTests(Case):
    COSMETIC = (
        ("emoticon added", BASE + " :)"),
        ("punctuation changed", "[support] please check the dummy report!!"),
        ("punctuation removed", "support please check the dummy report"),
        ("line breaks and a full stop", "[support]  please check\nthe dummy\n\nreport.  "),
        ("emoji added", BASE + " \U0001F44D\U0001F3FD"),
        ("emoji with presentation selector", BASE + " \U00002764\U0000FE0F"),
        ("emoji shortcode added", BASE + " :thumbsup:"),
        ("shortcode run with skin tone", BASE + " :wave::skin-tone-2:"),
        ("case changed", "[SUPPORT] Please Check The Dummy Report"),
        ("numbered emoji code added", BASE + " :100:"),
        ("trailing tilde", BASE + "~"),
        ("full width letters", "[support] please check the ｄｕｍｍｙ report"),
    )
    CONTENT = (
        ("changed digit", "[support] please check the dummy report 2"),
        ("added time", "[support] please check the dummy report at 10:31"),
        ("one changed word", "[support] please check the dummy summary"),
        ("added negation", "[support] please do not check the dummy report"),
        ("added mention", BASE + f" <@{OTHER_USER}>"),
        ("added link", BASE + f" <{LINK}|dummy page>"),
        ("added channel reference", BASE + " <#C0000000009|general>"),
        ("joined words", "[support] please check the dummyreport"),
        ("operator added", "[support] please check the dummy - report"),
        ("hangul word added", BASE + " 확인"),
    )

    def acked_root(self, text=BASE, **options):
        self.build(**options)
        result = self.pipeline.handle_event(event(text=text, channel=TAGGED))
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        self.store.set_state(result.message_id, "acked")
        self.now += 100  # past the wake debounce
        return result.message_id

    def outcome(self, mid):
        return dict(offers=self.offers.call_count, records=len(self.records()),
                    state=self.row(mid)["state"], body=self.row(mid)["body"],
                    ignored=self.pipeline.edits_ignored, woken=self.pipeline.edits_woken,
                    cosmetic=self.pipeline.edits_cosmetic)

    def test_cosmetic_edits_are_ignored_with_the_option(self):
        for label, text in self.COSMETIC:
            with self.subTest(label):
                mid = self.acked_root(cosmetic=True)
                with self.assertLogs("slack_dispatch.pipeline", level="INFO") as logs:
                    result = self.pipeline.handle_event(edit(2, text, channel=TAGGED))
                self.assertEqual((result.state, result.lane), ("acked", "support"))
                self.assertEqual(self.outcome(mid), dict(offers=1, records=1, state="acked",
                                                         body=text, ignored=1, woken=0,
                                                         cosmetic=1))
                self.assertIn(f"Edit recorded without wake id={mid} cosmetic", logs.output[-1])
                snapshot = self.pipeline.snapshot()
                self.assertEqual((snapshot["edits_cosmetic"], snapshot["edits_ignored"]), (1, 1))
                self.human.notify.assert_not_called()

    def test_cosmetic_edits_are_redelivered_without_the_option(self):
        for label, text in self.COSMETIC:
            with self.subTest(label):
                mid = self.acked_root()
                self.pipeline.handle_event(edit(2, text, channel=TAGGED))
                self.assertEqual(self.outcome(mid), dict(offers=2, records=2, state="assigned",
                                                         body=text, ignored=0, woken=1,
                                                         cosmetic=0))
                self.assertEqual(self.records()[-1]["kind"], "followup")
                self.assertNotIn("edits_cosmetic", self.pipeline.snapshot())

    def test_content_edits_are_always_redelivered(self):
        for cosmetic in (True, False):
            for label, text in self.CONTENT:
                with self.subTest(label, cosmetic=cosmetic):
                    mid = self.acked_root(cosmetic=cosmetic)
                    self.pipeline.handle_event(edit(2, text, channel=TAGGED))
                    got = self.outcome(mid)
                    self.assertEqual(got, dict(offers=2, records=2, state="assigned", body=text,
                                               ignored=0, woken=1, cosmetic=0))
                    self.assertEqual(self.records()[-1]["kind"], "followup")

    def test_changed_mention_and_link_targets_are_redelivered(self):
        cases = ((f"[support] ask <@{OTHER_USER}>", f"[support] ask <@{MENTION_USER}>"),
                 (f"[support] see <{LINK}/a|page>", f"[support] see <{LINK}/b|page>"),
                 (f"[support] see <{LINK}|page>", "[support] see page"))
        for old, new in cases:
            with self.subTest(new=new):
                mid = self.acked_root(old, cosmetic=True)
                self.pipeline.handle_event(edit(2, new, channel=TAGGED))
                got = self.outcome(mid)
                self.assertEqual((got["offers"], got["records"], got["body"], got["cosmetic"]),
                                 (2, 2, new, 0))

    def test_relabelled_link_and_mention_are_cosmetic(self):
        old = f"[support] see <{LINK}|page> and ask <@{OTHER_USER}|first>"
        new = f"[support] see <{LINK}|another label>, and ask <@{OTHER_USER}|second>!"
        mid = self.acked_root(old, cosmetic=True)
        self.pipeline.handle_event(edit(2, new, channel=TAGGED))
        self.assertEqual(self.outcome(mid)["cosmetic"], 1)
        self.assertEqual(self.row(mid)["body"], new)

    def test_whitespace_only_and_broadcast_only_edits_are_not_text_changes(self):
        """P24 compares the comparable_text form, which collapses whitespace and drops
        broadcasts: such edits were ignored before P37 and stay ignored with or without the
        option (counted in edits_ignored, not edits_cosmetic)."""
        for cosmetic in (True, False):
            for text in ("[support]  please check\nthe dummy\n\nreport  ", BASE + " <!here>"):
                with self.subTest(text=text, cosmetic=cosmetic):
                    mid = self.acked_root(cosmetic=cosmetic)
                    self.pipeline.handle_event(edit(2, text, channel=TAGGED))
                    self.assertEqual(self.outcome(mid), dict(
                        offers=1, records=1, state="acked", body=text, ignored=1, woken=0,
                        cosmetic=0))

    def test_unchanged_text_still_counts_as_ignored_only(self):
        mid = self.acked_root(cosmetic=True)
        self.pipeline.handle_event(edit(2, BASE, channel=TAGGED))
        self.assertEqual(self.outcome(mid), dict(offers=1, records=1, state="acked", body=BASE,
                                                 ignored=1, woken=0, cosmetic=0))

    def test_policy_always_with_the_option(self):
        mid = self.acked_root(cosmetic=True, policy="always")
        self.pipeline.handle_event(edit(2, BASE + " :)", channel=TAGGED))
        self.assertEqual(self.outcome(mid)["cosmetic"], 1)
        self.assertEqual(self.offers.call_count, 1)
        self.pipeline.handle_event(edit(3, BASE + " now", channel=TAGGED))
        self.assertEqual((self.offers.call_count, len(self.records())), (2, 2))

    def test_policy_never_is_unchanged(self):
        mid = self.acked_root(cosmetic=True, policy="never")
        self.pipeline.handle_event(edit(2, BASE + " :)", channel=TAGGED))
        self.pipeline.handle_event(edit(3, BASE + " now", channel=TAGGED))
        self.assertEqual(self.outcome(mid), dict(offers=1, records=1, state="acked",
                                                 body=BASE + " now", ignored=2, woken=0,
                                                 cosmetic=0))

    def test_assigned_item_edit_stays_ignored_without_counting_cosmetic(self):
        self.build(cosmetic=True)
        mid = self.pipeline.handle_event(event(text=BASE, channel=TAGGED)).message_id
        self.now += 100
        self.pipeline.handle_event(edit(2, BASE + " :)", channel=TAGGED))
        self.assertEqual(self.outcome(mid), dict(offers=1, records=1, state="assigned",
                                                 body=BASE + " :)", ignored=1, woken=0,
                                                 cosmetic=0))

    def test_missing_stored_body_is_never_cosmetic(self):
        mid = self.acked_root(cosmetic=True, store_body=False)
        self.pipeline.handle_event(edit(2, BASE + " :)", channel=TAGGED))
        got = self.outcome(mid)
        self.assertEqual((got["offers"], got["records"], got["cosmetic"], got["body"]),
                         (2, 2, 0, None))

    def test_heartbeat_has_no_cosmetic_counter_by_default(self):
        self.acked_root()
        self.assertNotIn("edits_cosmetic", self.pipeline.snapshot())


class CosmeticHelperTests(unittest.TestCase):
    def test_cosmetic_and_content_pairs(self):
        base = "please check the dummy report"
        for new in (base + ".", base.upper(), base + " :)", base + " \U0001F600",
                    base + " :+1:", "Please check the dummy report!!!"):
            with self.subTest(new=new):
                self.assertTrue(cosmetic_edit(base, new))
        for new in (base + " 2", base + " now", "please check the dummy reports",
                    base.replace("please", "please do not"), base + " <!channel>",
                    base + " :-1: 1", "please, check the dummy-report", base + "\U00003002"):
            with self.subTest(new=new):
                self.assertFalse(cosmetic_edit(base, new))

    def test_scripts_and_digits_count_as_content(self):
        self.assertTrue(cosmetic_edit("확인 부탁드립니다", "확인 부탁드립니다!!"))
        self.assertFalse(cosmetic_edit("확인 부탁드립니다", "확인 안 부탁드립니다"))
        self.assertFalse(cosmetic_edit("총 3건", "총 4건"))
        self.assertFalse(cosmetic_edit("at 10:30:45", "at 10:31:45"))
        self.assertFalse(cosmetic_edit("a:b:c", "a:b:d"))
        # A combining mark is content (it changes the letter it sits on).
        self.assertFalse(cosmetic_edit("e\U00000301x", "ex"))

    def test_blocks_and_raw_forms_agree(self):
        rendered = f"see {LINK} & <@{OTHER_USER}> please"
        raw = f"see <{LINK}|dummy page> &amp; <@{OTHER_USER}|dummy> please :)"
        self.assertTrue(cosmetic_edit(rendered, raw))
        self.assertTrue(cosmetic_edit(f"see {LINK}.", f"see <{LINK}|page>!"))

    def test_missing_sides(self):
        self.assertFalse(cosmetic_edit(None, "dummy"))
        self.assertFalse(cosmetic_edit("dummy", None))

    def test_parts_and_tokens(self):
        plain, entities = edit_parts(f"hi <@{OTHER_USER}|x> see <{LINK}|label>, <!here> "
                                     "<#C0000000009|general>")
        self.assertEqual(sorted(entities),
                         sorted(["!here", "@" + OTHER_USER, "#C0000000009", LINK]))
        self.assertNotIn("label", plain)
        content, emoji = cosmetic_tokens("Hello, World! :smile: (^_^) ... 1.5 -> x "
                                         "\U0001F44D\U0001F3FD :wave::skin-tone-2:")
        self.assertEqual(content, ["hello", "world", "1.5", "->", "x"])
        self.assertEqual(sorted(emoji), sorted([":smile:", "\U0001F44D\U0001F3FD",
                                                ":wave::skin-tone-2:"]))

    def test_canonical_token(self):
        for token, expected in (("done.", "done"), ("done!!!", "done"), ("(note)", "note"),
                                ("'quoted'", "quoted"), ("re:", "re"), ("5.", "5."),
                                ("1.5", "1.5"), ("(5)", "(5)"), ("don't", "don't"),
                                ("~ok~", "ok"), ("-5", "-5"), (":", "")):
            with self.subTest(token=token):
                self.assertEqual(canonical_token(token), expected)

    def test_linear_time(self):
        import time
        text = ("word 1.5 :smile: (^_^) " * 50000) + ":a" * 100000
        started = time.monotonic()
        self.assertTrue(cosmetic_edit(text, text + " !!!"))
        self.assertLess(time.monotonic() - started, 10.0)


class CosmeticRuleTableTests(unittest.TestCase):
    """P37 fix1: the token based rule (verifier findings 1 and 2)."""
    NOT_COSMETIC = (
        # Finding 1: punctuation next to digits, operators and word boundaries.
        ("pay $1.5 now", "pay $15 now"), ("0.5", "05"), ("1,000", "1.000"),
        ("due 12/3", "due 1/23"), ("at 10:30", "at 1:030"), ("temp -5", "temp 5"),
        ("I agree +1", "I agree -1"), ("x > 5", "x < 5"), ("5 >= 3", "5 <= 3"),
        ("no body", "nobody"), ("ok :+1:", "ok :-1:"),
        # Finding 2: shortcodes that carry words.
        ("ok :yes:", "ok :no:"), ("meet 3 :pm:", "meet 3 :am:"), ("approve :x:", "approve"),
        (":+1:", ":-1:"), ("meet :tomorrow:", "meet :today:"),
        # Fix pass additions.
        ("a - b", "a b"), ("5 - 3", "5 3"), ("A -> B", "A B"), ("yes/no", "yes no"),
        ("re: plan", "replan"), ("ok \U0001F44D", "ok"),
        ("ok \U0001F44D", "ok \U0001F44E"), ("total 5.", "total 5"), ("don't", "dont"),
        # Earlier must-not cases.
        ("report 1", "report 2"), ("ship now", "ship later"), ("do it", "do not do it"),
        ("do it", "don't do it"), ("확인 부탁드립니다", "확인하지 마세요"),
        ("하세요", "하지 마세요"), ("<@U0000000001> hi", "<@U0000000002> hi"),
        ("hi", "hi <@U0000000001>"), ("<@U0000000001> hi", "hi"),
        ("hi <#C0000000001>", "hi <#C0000000002>"), ("hi", "hi <!here>"),
        (f"see <{LINK}/x|a>", f"see <{LINK}/y|a>"), (f"see <{LINK}|a>", "see a"),
    )
    COSMETIC = (
        ("done", "done =)"), ("done", "done :)"), ("done", "done ^^"), ("done", "done (^_^)"),
        ("done", "done !!!"), ("done", "done \U0001F44D"), ("done", "done :smile:"),
        ("done", "done :wave::skin-tone-2:"), ("Done.", "done!!!"), ("Done.", "Done"),
        ("a b", "a\n\nb  "), ("a b", "  a   b"), ("ok", "OK"), ("ok", "ok~"), ("ok", "ok ~"),
        (f"see <{LINK}|here>", f"see <{LINK}|there>"),
        ("<@U0000000001|first> hi", "<@U0000000001|second> hi"),
        ("ok \U0001F44D", "ok \U0001F44D \U0001F389"),
        ("ok \U00002764", "ok \U00002764\U0000FE0F"), ("done", "done... !?"),
    )

    def test_not_cosmetic(self):
        for old, new in self.NOT_COSMETIC:
            with self.subTest(old=old, new=new):
                self.assertFalse(cosmetic_edit(old, new))

    def test_cosmetic(self):
        for old, new in self.COSMETIC:
            with self.subTest(old=old, new=new):
                self.assertTrue(cosmetic_edit(old, new))


class ConfigTests(unittest.TestCase):
    def test_default_sample_and_validation(self):
        raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        self.assertFalse(load_config(SAMPLE).wake_policy.followup_ignore_cosmetic)
        raw["wake_policy"].pop("followup_ignore_cosmetic", None)
        self.assertFalse(parse_config(raw).wake_policy.followup_ignore_cosmetic)
        for value in (True, False):
            raw["wake_policy"]["followup_ignore_cosmetic"] = value
            self.assertIs(parse_config(raw).wake_policy.followup_ignore_cosmetic, value)
        for value in ("true", 1, 0, None, "", ["true"]):
            with self.subTest(value=value):
                bad = deepcopy(raw)
                bad["wake_policy"]["followup_ignore_cosmetic"] = value
                with self.assertRaises(ConfigError) as caught:
                    parse_config(bad)
                self.assertIn("wake_policy.followup_ignore_cosmetic", str(caught.exception))

    def test_helper_fallback(self):
        self.assertTrue(followup_ignore_cosmetic(Mock(wake_policy=Mock(
            followup_ignore_cosmetic=True))))
        self.assertFalse(followup_ignore_cosmetic(Mock(wake_policy=object())))
        self.assertFalse(followup_ignore_cosmetic(object()))


class MentionAddedTests(Case):
    def excluded_post(self, number=1, text="dummy request", **changes):
        result = self.pipeline.handle_event(event(number, **dict(
            {"channel": MENTION_ONLY, "user": PLAIN, "text": text}, **changes)))
        row = self.store.get_message(result.message_id)
        self.assertEqual((row["state"], row["note"]), ("excluded", NO_MENTION))
        self.now += 5
        return result.message_id

    def snapshot(self):
        return dict(offers=self.offers.call_count, lane_notify=self.lane.notify.call_count,
                    human_notify=self.human.notify.call_count, records=len(self.all_records()))

    def assert_assigned_once(self, mid, result):
        row = self.store.get_message(mid)
        self.assertEqual((result.state, result.lane), ("assigned", DEFAULT_LANE))
        self.assertEqual((row["state"], row["lane"], row["route_reason"], row["attention"]),
                         ("assigned", DEFAULT_LANE, "channel_default", "owner_mention"))
        records = self.records(DEFAULT_LANE)
        self.assertEqual([record["id"] for record in records], [mid])
        self.assertNotIn("edit_delta", records[0])
        self.assertEqual((self.offers.call_count, self.lane.notify.call_count), (1, 1))
        self.assertEqual(self.offers.call_args.args[0], DEFAULT_LANE)
        self.assertEqual(self.offers.call_args.args[1]["attention"], "owner_mention")
        self.assertEqual(self.notices(mid), ["attention:owner_mention"])
        self.assertEqual(self.store.lookup_binding("slack", MENTION_ONLY, row["ts"]),
                         DEFAULT_LANE)
        counters = self.pipeline.snapshot()
        self.assertEqual((counters["edits_mention_added"], counters["excluded_no_mention"]),
                         (1, 1))
        self.assertNotIn("edits_woken", counters)

    def test_added_mention_or_name_is_a_first_assignment(self):
        texts = (("exact", f"<@{OWNER}> dummy request"),
                 ("mention user", f"<@{MENTION_USER}> dummy request"),
                 ("name", f"{NAME}, dummy request"))
        for policy in POLICIES:
            for owner_mode in (False, True):
                for label, text in texts:
                    with self.subTest(policy=policy, owner_mode=owner_mode, kind=label):
                        self.build(policy=policy, owner_mode=owner_mode, names=(NAME,))
                        mid = self.excluded_post()
                        result = self.pipeline.handle_event(edit(2, text, channel=MENTION_ONLY,
                                                                 user=PLAIN))
                        self.assert_assigned_once(mid, result)
                        self.assertEqual(self.row(mid)["body"], text)

    def test_second_identical_edit_does_nothing(self):
        for policy in ("changed_after_ack", "never"):
            with self.subTest(policy=policy):
                self.build(policy=policy)
                mid = self.excluded_post()
                text = f"<@{OWNER}> dummy request"
                self.pipeline.handle_event(edit(2, text, channel=MENTION_ONLY, user=PLAIN))
                before = self.snapshot()
                self.now += 100
                result = self.pipeline.handle_event(edit(3, text, channel=MENTION_ONLY,
                                                         user=PLAIN))
                self.assertEqual((result.state, result.lane), ("assigned", DEFAULT_LANE))
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.notices(mid), ["attention:owner_mention"])
                self.assertEqual(self.pipeline.edits_mention_added, 1)

    def test_second_identical_edit_under_always_is_not_a_second_assignment(self):
        """Under always the item now holds a lane, so the P22/P24 rule for such items
        applies (every edit re-delivers a followup); the mention path does not run again."""
        self.build(policy="always")
        mid = self.excluded_post()
        text = f"<@{OWNER}> dummy request"
        self.pipeline.handle_event(edit(2, text, channel=MENTION_ONLY, user=PLAIN))
        self.now += 100
        self.pipeline.handle_event(edit(3, text, channel=MENTION_ONLY, user=PLAIN))
        self.assertEqual([(record["id"], record["kind"]) for record in self.records(DEFAULT_LANE)],
                         [(mid, "followup"), (mid, "followup")])
        counters = self.pipeline.snapshot()
        self.assertEqual((counters["edits_mention_added"], counters["edits_woken"]), (1, 1))

    def test_reply_in_an_unbound_thread(self):
        self.build()
        mid = self.excluded_post(2, ts="1000.000002", thread_ts="1000.000001",
                                 is_thread_reply=True)
        result = self.pipeline.handle_event(edit(3, f"<@{OWNER}> dummy request",
                                                 channel=MENTION_ONLY, user=PLAIN,
                                                 ts="1000.000002", thread_ts="1000.000001",
                                                 is_thread_reply=True))
        self.assertEqual((result.state, result.lane), ("assigned", DEFAULT_LANE))
        self.assertEqual(self.store.lookup_binding("slack", MENTION_ONLY, "1000.000001"),
                         DEFAULT_LANE)
        self.assertEqual([record["id"] for record in self.records(DEFAULT_LANE)], [mid])

    def test_edit_without_a_mention_keeps_today_s_behaviour(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                self.build(policy=policy, names=(NAME,))
                mid = self.excluded_post()
                result = self.pipeline.handle_event(edit(2, "dummy request, corrected",
                                                         channel=MENTION_ONLY, user=PLAIN))
                row = self.store.get_message(mid)
                self.assertEqual((result.state, row["state"], row["note"]),
                                 ("excluded", "excluded", NO_MENTION))
                self.assertEqual(self.all_records(), [])
                self.assertNotIn("edits_mention_added", self.pipeline.snapshot())

    def test_ledger_note_after_the_assignment(self):
        """Routing never rewrites the note: the assigned item keeps `excluded:no_mention`
        (finding 5 of the verifier; this pins what the code does)."""
        self.build()
        mid = self.excluded_post()
        self.pipeline.handle_event(edit(2, f"<@{OWNER}> dummy request", channel=MENTION_ONLY,
                                        user=PLAIN))
        row = self.store.get_message(mid)
        self.assertEqual((row["state"], row["lane"], row["note"]),
                         ("assigned", DEFAULT_LANE, NO_MENTION))

    def test_counter_counts_assignments_only(self):
        """P37 fix1 (finding 6): an edit that adds the mention but whose routing ends
        elsewhere does not count in edits_mention_added."""
        # A mention-only channel without a default lane: owner_mention queues for a human.
        self.build()
        self.config = replace(self.config, channel_defaults=dict(
            self.config.channel_defaults, **{MENTION_ONLY: None}))
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(),
                                 [FileInboxNotifier(self.config.inbox.root,
                                                    self.store.get_message),
                                  self.lane, self.human],
                                 WakePolicyEngine(self.config.wake_policy, lambda: self.now),
                                 lambda: self.now)
        mid = self.excluded_post()
        result = self.pipeline.handle_event(edit(2, f"<@{OWNER}> dummy request",
                                                 channel=MENTION_ONLY, user=PLAIN))
        self.assertEqual((result.state, self.store.get_message(mid)["attention"]),
                         ("human_pending", "owner_mention"))
        self.assertNotIn("edits_mention_added", self.pipeline.snapshot())
        # The edit event carries the operator as its user: the owner post path closes it.
        self.build()
        mid = self.excluded_post()
        result = self.pipeline.handle_event(edit(2, f"<@{OWNER}> dummy request",
                                                 channel=MENTION_ONLY, user=OWNER))
        self.assertEqual((result.state, self.store.get_message(mid)["note"]),
                         ("done", "owner_post"))
        self.assertNotIn("edits_mention_added", self.pipeline.snapshot())

    def test_old_text_that_already_had_the_name(self):
        """The post was excluded before the name was configured; an edit that keeps the
        name adds nothing, so it is not routed (with a stored body to compare)."""
        self.build(names=())
        mid = self.excluded_post(text=f"{NAME}, dummy request")
        # The same instance restarted with the name configured.
        self.config = replace(self.config, always_human=replace(self.config.always_human,
                                                                mention_texts=(NAME,)))
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(),
                                 [FileInboxNotifier(self.config.inbox.root,
                                                    self.store.get_message),
                                  self.lane, self.human],
                                 WakePolicyEngine(self.config.wake_policy, lambda: self.now),
                                 lambda: self.now)
        result = self.pipeline.handle_event(edit(2, f"{NAME}, dummy request corrected",
                                                 channel=MENTION_ONLY, user=PLAIN))
        row = self.store.get_message(mid)
        self.assertEqual((result.state, row["state"], row["note"]),
                         ("excluded", "excluded", NO_MENTION))
        self.assertEqual(self.all_records(), [])
        self.assertNotIn("edits_mention_added", self.pipeline.snapshot())

    def test_without_a_stored_body_the_note_stands_for_the_old_text(self):
        self.build(store_body=False)
        mid = self.excluded_post()
        result = self.pipeline.handle_event(edit(2, f"<@{OWNER}> dummy request",
                                                 channel=MENTION_ONLY, user=PLAIN))
        self.assert_assigned_once(mid, result)

    def test_other_exclusions_stay_excluded(self):
        mention = f"<@{OWNER}> dummy request"
        for policy in POLICIES:
            with self.subTest(policy=policy, reason="excluded channel"):
                self.build(policy=policy)
                mid = self.pipeline.handle_event(
                    event(1, channel=EXCLUDED, text="dummy")).message_id
                self.now += 5
                self.pipeline.handle_event(edit(2, mention, channel=EXCLUDED))
                self.assert_still_excluded(mid, "excluded")
            with self.subTest(policy=policy, reason="bot"):
                self.build(policy=policy)
                mid = self.pipeline.handle_event(event(1, channel=MENTION_ONLY, text="dummy",
                                                       is_bot=True)).message_id
                self.now += 5
                self.pipeline.handle_event(edit(2, mention, channel=MENTION_ONLY, is_bot=True))
                self.assert_still_excluded(mid, "excluded:bot")
            with self.subTest(policy=policy, reason="not a member"):
                self.build(policy=policy)
                self.pipeline.membership = Mock(excludes=Mock(return_value=True))
                mid = self.pipeline.handle_event(
                    event(1, channel=NORMAL, text="dummy")).message_id
                self.pipeline.membership = None  # the operator joined the channel since
                self.now += 5
                self.pipeline.handle_event(edit(2, mention, channel=NORMAL))
                self.assert_still_excluded(mid, NOT_MEMBER)
            for channel in (NORMAL, MENTION_ONLY):
                with self.subTest(policy=policy, reason="operator", channel=channel):
                    self.build(policy=policy)
                    mid = self.pipeline.handle_event(event(1, channel=channel,
                                                           text="dummy")).message_id
                    actions.exclude(self.store, mid)
                    self.now += 5
                    records = len(self.all_records())
                    self.pipeline.handle_event(edit(2, mention, channel=channel))
                    self.assert_still_excluded(mid, actions.REASON, records)

    def assert_still_excluded(self, mid, note, records=0):
        row = self.store.get_message(mid, include_body=True)
        self.assertEqual((row["state"], row["note"]), ("excluded", note))
        self.assertEqual(row["body"], f"<@{OWNER}> dummy request")
        self.assertEqual(len(self.all_records()), records)
        self.assertNotIn("edits_mention_added", self.pipeline.snapshot())

    def test_edit_that_removes_a_mention_changes_nothing(self):
        for policy in ("changed_after_ack", "never"):
            with self.subTest(policy=policy):
                self.build(policy=policy)
                result = self.pipeline.handle_event(event(1, channel=MENTION_ONLY, user=PLAIN,
                                                          text=f"<@{OWNER}> dummy request"))
                self.assertEqual(result.state, "assigned")
                before = self.snapshot()
                self.now += 100
                edited = self.pipeline.handle_event(edit(2, "dummy request",
                                                         channel=MENTION_ONLY, user=PLAIN))
                row = self.store.get_message(result.message_id)
                self.assertEqual((edited.state, row["state"], row["lane"], row["attention"]),
                                 ("assigned", "assigned", DEFAULT_LANE, "owner_mention"))
                self.assertEqual(self.snapshot(), before)
                self.assertNotIn("edits_mention_added", self.pipeline.snapshot())

    def test_edit_that_removes_a_mention_under_always_keeps_the_lane(self):
        self.build(policy="always")
        result = self.pipeline.handle_event(event(1, channel=MENTION_ONLY, user=PLAIN,
                                                  text=f"<@{OWNER}> dummy request"))
        self.now += 100
        self.pipeline.handle_event(edit(2, "dummy request", channel=MENTION_ONLY, user=PLAIN))
        row = self.store.get_message(result.message_id)
        self.assertEqual((row["state"], row["lane"]), ("assigned", DEFAULT_LANE))
        self.assertNotIn("edits_mention_added", self.pipeline.snapshot())


class DeltaCase(Case):
    def acked_root(self, text="[support] alpha beta gamma", **options):
        self.build(**options)
        mid = self.pipeline.handle_event(event(text=text, channel=TAGGED)).message_id
        self.store.set_state(mid, "acked")
        self.now += 100
        return mid

    def redeliver(self, mid, number, text):
        self.store.set_state(mid, "acked")
        self.now += self.config.service.followup_window_seconds + 1
        self.pipeline.handle_event(edit(number, text, channel=TAGGED))
        return self.records()[-1]


class EditDeltaTests(DeltaCase):
    def test_add_remove_and_replace(self):
        mid = self.acked_root()
        added = self.redeliver(mid, 2, "[support] alpha beta gamma delta")
        self.assertEqual((added["kind"], added["edit_delta"]),
                         ("followup", {"added": 1, "removed": 0}))
        removed = self.redeliver(mid, 3, "[support] alpha gamma delta")
        self.assertEqual(removed["edit_delta"], {"added": 0, "removed": 1})
        replaced = self.redeliver(mid, 4, "[support] alpha omega delta")
        self.assertEqual(replaced["edit_delta"], {"added": 1, "removed": 1})
        rewritten = self.redeliver(mid, 5, "[support] something else entirely now")
        self.assertEqual(rewritten["edit_delta"], {"added": 4, "removed": 3})
        for row in self.records():
            self.assertLessEqual(set(row), RECORD_KEYS | {"edit_delta"})
            self.assertNotIn("omega", json.dumps(row))
        self.assertNotIn("edit_delta", self.records()[0])

    def test_entities_are_tokens_and_labels_do_not_count(self):
        mid = self.acked_root(f"[support] ask <@{OTHER_USER}|first last> about <{LINK}|page>")
        same = self.redeliver(mid, 2, f"[support] ask <@{OTHER_USER}> about <{LINK}|new label> x")
        self.assertEqual(same["edit_delta"], {"added": 1, "removed": 0})
        other = self.redeliver(mid, 3, f"[support] ask <@{MENTION_USER}> about <{LINK}|x> x")
        self.assertEqual(other["edit_delta"], {"added": 1, "removed": 1})

    def test_redelivery_under_always_and_the_routing_path(self):
        self.build(policy="always")
        mid = self.pipeline.handle_event(event(text="no tag here", channel=TAGGED)).message_id
        self.assertEqual(self.row(mid)["state"], "human_pending")
        self.now += 100
        self.pipeline.handle_event(edit(2, "[support] no tag here", channel=TAGGED))
        self.assertEqual(self.records()[-1]["edit_delta"], {"added": 1, "removed": 0})
        self.now += 100
        self.pipeline.handle_event(edit(3, "[support] no tag here", channel=TAGGED))
        self.assertEqual(self.records()[-1]["edit_delta"], {"added": 0, "removed": 0})

    def test_absent_without_a_stored_body_and_on_first_assignments(self):
        mid = self.acked_root(store_body=False)
        self.pipeline.handle_event(edit(2, "[support] alpha beta", channel=TAGGED))
        records = self.records()
        self.assertEqual([row["kind"] for row in records], ["new", "followup"])
        self.assertTrue(all("edit_delta" not in row for row in records))
        self.assertEqual(self.row(mid)["state"], "assigned")
        # A first assignment by an edit (human_pending gains a tag) carries no delta.
        self.build()
        queued = self.pipeline.handle_event(event(text="no tag", channel=TAGGED)).message_id
        self.now += 100
        self.pipeline.handle_event(edit(2, "[support] now tagged", channel=TAGGED))
        self.assertEqual([row["id"] for row in self.records()], [queued])
        self.assertNotIn("edit_delta", self.records()[0])

    def test_no_delta_left_behind_after_the_write(self):
        mid = self.acked_root()
        self.redeliver(mid, 2, "[support] alpha beta gamma delta")
        self.assertEqual(self.pipeline._edit_deltas, {})
        notifier = self.pipeline.notifiers[0]
        self.assertNotIn("edit_delta", notifier.lookup(mid))

    def test_helper(self):
        self.assertEqual(edit_delta("a b c", "a b c d"), {"added": 1, "removed": 0})
        self.assertEqual(edit_delta("a b c", "a b"), {"added": 0, "removed": 1})
        self.assertEqual(edit_delta("a b c", "a x c"), {"added": 1, "removed": 1})
        self.assertEqual(edit_delta("a a b", "a b b"), {"added": 1, "removed": 1})
        self.assertEqual(edit_delta("a\nb", " a  b "), {"added": 0, "removed": 0})
        self.assertEqual(edit_delta(f"hi <@{OTHER_USER}|two words>", f"hi <@{OTHER_USER}>"),
                         {"added": 0, "removed": 0})
        self.assertIsNone(edit_delta(None, "a"))
        self.assertIsNone(edit_delta("a", None))


class RecordFieldTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p37-files-")
        self.addCleanup(temp.cleanup)
        self.root = temp.name
        self.message = {"id": "slack:C0000000002:1000.000001", "channel": "C0000000002",
                        "ts": "1000.000001", "edited_at": "2026-11-19T00:00:00+00:00"}

    def test_record_keeps_a_valid_delta_on_followups_only(self):
        row = record(dict(self.message, edit_delta={"added": 2, "removed": 0}))
        self.assertEqual((row["kind"], row["edit_delta"]), ("followup", {"added": 2, "removed": 0}))
        self.assertNotIn("edit_delta", record(dict(self.message, edit_delta={"added": 1,
                                                                             "removed": 0}),
                                              kind="reroute"))
        for bad in (None, {"added": 1}, {"added": -1, "removed": 0}, {"added": True, "removed": 0},
                    {"added": 1, "removed": 0, "text": "x"}, [1, 0]):
            with self.subTest(bad=bad):
                self.assertNotIn("edit_delta", record(dict(self.message, edit_delta=bad)))

    def test_append_round_trip_and_validation(self):
        good = record(dict(self.message, edit_delta={"added": 1, "removed": 3}))
        plain = record(dict(self.message, edited_at=None))
        append(self.root, "support", [good, plain])
        rows = read(self.root, "support", include_done=True)
        self.assertEqual(rows[0]["edit_delta"], {"added": 1, "removed": 3})
        self.assertNotIn("edit_delta", rows[1])
        for bad in (dict(good, edit_delta={"added": "1", "removed": 0}),
                    dict(plain, edit_delta={"added": 1, "removed": 0})):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    append(self.root, "support", [bad])
        self.assertEqual(len(read(self.root, "support", include_done=True)), 2)


class CliTests(DeltaCase):
    def edited_and_plain(self):
        mid = self.acked_root()
        self.redeliver(mid, 2, "[support] alpha beta gamma delta")
        self.store.set_state(mid, "acked")
        other = self.pipeline.handle_event(event(9, text="[support] other dummy",
                                                 channel=TAGGED)).message_id
        return mid, other

    def test_text_list_output(self):
        mid, other = self.edited_and_plain()
        lines = self.cli("inbox", "support").splitlines()
        self.assertTrue(lines[0].startswith("id age when channel thread kind edited"))
        self.assertNotIn("edit_delta", lines[0])
        rows = {line.split()[0]: line.split() for line in lines[1:]}
        self.assertEqual(rows[mid][-3:], ["edit", "+1", "-0"])
        self.assertEqual(len(rows[mid]), len(rows[other]) + 3)
        self.assertNotIn("edit", rows[other])

    def test_latest_followup_record_decides(self):
        mid, _ = self.edited_and_plain()
        self.redeliver(mid, 3, "[support] alpha gamma")
        rows = json.loads(self.cli("inbox", "support", "--json"))
        self.assertEqual({row["id"]: row.get("edit_delta") for row in rows}[mid],
                         {"added": 0, "removed": 2})

    def test_json_list_output(self):
        mid, other = self.edited_and_plain()
        rows = {row["id"]: row for row in json.loads(self.cli("inbox", "support", "--json"))}
        self.assertEqual(rows[mid]["edit_delta"], {"added": 1, "removed": 0})
        self.assertEqual(rows[mid]["edited"], "yes")
        self.assertNotIn("edit_delta", rows[other])

    def test_thread_views(self):
        mid, other = self.edited_and_plain()
        text = self.cli("inbox", "support", "--thread", "--no-body")
        self.assertIn("kind new edited yes attention - edit +1 -0", text)
        self.assertEqual(text.count("edit +"), 1)
        groups = json.loads(self.cli("inbox", "support", "--thread", "--json", "--no-body"))
        roots = {group["root"]["id"]: group["root"] for group in groups}
        self.assertEqual(roots[mid]["edit_delta"], {"added": 1, "removed": 0})
        self.assertNotIn("edit_delta", roots[other])

    def test_thread_item_rows(self):
        self.build()
        root = self.pipeline.handle_event(event(1, text="[support] root", channel=TAGGED))
        self.now += 100
        reply = self.pipeline.handle_event(event(2, text="reply one", channel=TAGGED,
                                                 ts="1000.000002", thread_ts=ROOT_TS,
                                                 is_thread_reply=True)).message_id
        self.store.set_state(reply, "acked")
        self.now += 100
        self.pipeline.handle_event(edit(3, "reply one two", channel=TAGGED, ts="1000.000002",
                                        thread_ts=ROOT_TS, is_thread_reply=True))
        text = self.cli("inbox", "support", "--thread", "--no-body")
        item_line = [line for line in text.splitlines() if line.startswith(reply)][0]
        self.assertTrue(item_line.endswith("edit +1 -0"), item_line)
        groups = json.loads(self.cli("inbox", "support", "--thread", "--json", "--no-body"))
        items = [item for group in groups for item in group["items"]]
        self.assertEqual([item.get("edit_delta") for item in items],
                         [{"added": 1, "removed": 0}])
        self.assertNotIn("edit_delta", groups[0]["root"])
        self.assertEqual(groups[0]["root"]["id"], root.message_id)


if __name__ == "__main__":
    unittest.main()
