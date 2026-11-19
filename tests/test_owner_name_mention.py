"""P36 owner name mention: `always_human.mention_texts` raises `owner_mention`.

Offline only: the real SQLite ledger in a temporary directory, mock notifiers and a fixed
clock. Dummy ids and neutral sample names only.
"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import random
import tempfile
import time
import unittest
from unittest.mock import Mock

from slack_dispatch.config.loader import ConfigError, parse_config
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import Batch, Defer, Deliver, WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import NullSuggester
from slack_dispatch.router.engine import (
    NO_MENTION, attention_for, contains_name, exact_mention, fold_text, folded_names,
    name_mention, plain_segments, route,
)
from tests.fakes.fake_store import message
from tests.test_pipeline_integration import event, fixture_config
from tests.test_router_config import raw_config

MENTION_ONLY = "C0000000004"
NORMAL = "C0000000001"
DEFAULT_LANE = "owner"
OWNER = "U0000000093"
PROTECTED = "U0000000091"
PLAIN = "U0000000088"
NAMES = ("Operator Kim", "운영자님")


def with_names(config, names=NAMES):
    return replace(config, always_human=replace(config.always_human, mention_texts=tuple(names)))


class LoaderTests(unittest.TestCase):
    def parse(self, value):
        raw = deepcopy(raw_config())
        raw["always_human"]["mention_texts"] = value
        return parse_config(raw)

    def test_default_is_empty(self):
        self.assertEqual(parse_config(raw_config()).always_human.mention_texts, ())
        raw = raw_config()
        del raw["always_human"]
        self.assertEqual(parse_config(raw).always_human.mention_texts, ())

    def test_valid_list_is_trimmed(self):
        config = self.parse(["Operator Kim", "  운영자님 ", "ab"])
        self.assertEqual(config.always_human.mention_texts, ("Operator Kim", "운영자님", "ab"))
        self.assertEqual(self.parse([]).always_human.mention_texts, ())
        names = self.parse([f"name{i}" for i in range(20)]).always_human.mention_texts
        self.assertEqual(len(names), 20)

    def test_not_a_list(self):
        for value in ("Operator Kim", None, {"name": "Operator Kim"}, 3):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as caught:
                    self.parse(value)
                self.assertEqual(str(caught.exception), "always_human.mention_texts must be a list")

    def test_too_many_entries(self):
        with self.assertRaises(ConfigError) as caught:
            self.parse([f"name{i}" for i in range(21)])
        self.assertIn("at most 20 entries", str(caught.exception))

    def test_bad_entries(self):
        for value in ("", "a", " a ", "   ", "\t\n", 1, None, ["Operator Kim"], True):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as caught:
                    self.parse(["Operator Kim", value])
                text = str(caught.exception)
                self.assertIn("always_human.mention_texts entries", text)
                self.assertIn("at least 2 characters", text)


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.config = with_names(parse_config(raw_config()))

    def named(self, text, names=NAMES):
        return name_mention({"text": text}, with_names(self.config, names))

    def test_case_insensitive(self):
        for text in ("operator kim, please check", "OPERATOR KIM please", "Hi Operator Kim."):
            with self.subTest(text=text):
                self.assertTrue(self.named(text))
        self.assertTrue(self.named("hi operator kim", ("OPERATOR KIM",)))

    def test_nfkc_full_width_forms(self):
        self.assertTrue(self.named("Ｏｐｅｒａｔｏｒ　Ｋｉｍ 확인 부탁드립니다"))
        self.assertTrue(self.named("operator kim", ("Ｏｐｅｒａｔｏｒ Ｋｉｍ",)))

    def test_whitespace_runs_collapse(self):
        self.assertTrue(self.named("Operator   \n\t Kim please"))
        self.assertTrue(self.named("Operator Kim please"))
        self.assertTrue(self.named("operator kim", ("Operator    Kim",)))
        self.assertTrue(self.named("운영자님 확인 부탁드립니다", ("운영자님",)))

    def test_ascii_word_boundary(self):
        names = ("Sam",)
        for text in ("sample text", "Asam", "sam1", "1sam", "samsam"):
            with self.subTest(text=text):
                self.assertFalse(self.named(text, names))
        for text in ("Sam", "sam, please check", "(sam)", "hi sam!", "sam님 확인",
                     "sample then sam", "sam_x", "sam-x"):
            with self.subTest(text=text):
                self.assertTrue(self.named(text, names))
        self.assertFalse(self.named("Operator Kimberly", ("Operator Kim",)))
        self.assertTrue(self.named("Operator Kim님", ("Operator Kim",)))

    def test_non_ascii_names_match_as_substrings(self):
        self.assertTrue(self.named("김운영자님께서 확인 부탁"))
        self.assertTrue(self.named("x운영자님x"))
        self.assertFalse(self.named("운영자 확인"))

    def test_not_inside_slack_entities(self):
        for text in ("<https://example.com/operator-kim|Operator Kim>",
                     f"<@{PLAIN}|Operator Kim> please",
                     "<#C0000000001|operator kim>",
                     "<mailto:x@example.com|운영자님>",
                     "<!subteam^S0000000001|Operator Kim>"):
            with self.subTest(text=text):
                self.assertFalse(self.named(text))
        self.assertTrue(self.named("<https://example.com|link> Operator Kim"))
        self.assertTrue(self.named("Operator Kim <https://example.com|link>"))
        # A name split by an entity does not match across it.
        self.assertFalse(self.named("Operator <https://example.com|link> Kim"))
        # A `<` without a later `>` starts no entity.
        self.assertTrue(self.named("a &lt; b and < Operator Kim"))

    def test_segments_and_escapes(self):
        self.assertEqual(plain_segments("a <x|y> b &lt;c&gt; &amp;"), ["a ", " b <c> &"])
        self.assertEqual(fold_text("  Ａ　B\n c "), "a b c")
        self.assertTrue(self.named("R&amp;D team", ("R&D",)))

    def test_no_regex_from_config(self):
        self.assertFalse(self.named("axb", ("a.b",)))
        self.assertTrue(self.named("a.b", ("a.b",)))
        self.assertFalse(self.named("anything", ("(*[",)))
        self.assertTrue(self.named("x (*[ y", ("(*[",)))

    def test_long_text_completes(self):
        self.assertFalse(self.named("a" * 200000, ("aa", "a" * 50)))
        self.assertFalse(self.named("<" * 100000 + "x" * 100000))
        self.assertTrue(self.named("a" * 200000 + " Operator Kim"))

    def test_worst_case_is_fast(self):
        """fix1: 1 MB of `a` with 20 short names, distinct and repeated, stays far below 2 s
        (it took seconds when every occurrence went through a Python-level check)."""
        text = "a" * 1_000_000
        cases = (["aa"] * 20, ["a" * (i + 2) for i in range(20)],
                 [f"a{chr(98 + i)}" if i % 2 else "a" * (i + 2) for i in range(20)],
                 [f"name{i}" for i in range(20)])
        for names in cases:
            with self.subTest(names=names[:2]):
                start = time.perf_counter()
                self.assertFalse(self.named(text, names))
                self.assertLess(time.perf_counter() - start, 2.0)
        start = time.perf_counter()
        self.assertTrue(self.named(text + " aa", ["a" * (i + 2) for i in range(20)]))
        self.assertFalse(self.named(("a" * 999 + "\x00") * 1000, ["a" * (i + 2) for i in range(20)]))
        self.assertLess(time.perf_counter() - start, 2.0)

    def test_names_are_folded_and_deduped_once(self):
        names = folded_names(("Operator Kim", "OPERATOR   KIM", "Ｏｐｅｒａｔｏｒ Ｋｉｍ", "sam",
                              "Sam Lee", "운영자님", "김운영자님"))
        self.assertEqual(names.slow, (("operator kim", True), ("sam", True),
                                      ("sam lee", True), ("운영자님", False),
                                      ("김운영자님", False)))
        # `sam lee` and `김운영자님` are implied by `sam` and `운영자님`.
        self.assertEqual([key for key, _ in names.fast],
                         ["\x00operator\x00 \x00kim\x00", "\x00sam\x00", "운영자님"])
        self.assertTrue(names.fast_ok)
        self.assertIs(folded_names(("sam",)), folded_names(("sam",)))
        self.assertFalse(folded_names(("a\x00b",)).fast_ok)

    def test_fast_path_agrees_with_the_reference_rule(self):
        """fix1: the fast path gives the `contains_name` answer on seeded random texts,
        control characters and entities included."""
        alphabet = ["a", "b", "A", "1", " ", ".", "-", "_", "<", ">", "|", "&amp;", "&lt;",
                    "é", "가", "님", "ａ", "　", "\n", "\x00", "\x01", "\x02", "sam", "ab"]
        pool = ["ab", "a b", "sam", "Sam.", ".a", "a-1", "가님", "a가", "aa", "aaa", "b a",
                "1a", "ａb", "a\x00b", "x\x01", "a\x02", "ba", "a.b", "s a m"]
        rng = random.Random(36)
        for _ in range(5000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 14)))
            names = rng.sample(pool, rng.randint(1, 4))
            folded = list(dict.fromkeys((n, n.isascii()) for n in map(fold_text, names) if n))
            parts = [fold_text(part) for part in plain_segments(text)]
            expected = any(contains_name(part, name, bounded)
                           for name, bounded in folded for part in parts if part)
            self.assertEqual(self.named(text, names), expected, (text, names))

    def test_attention_for(self):
        self.assertEqual(attention_for(message(user=PLAIN, text="Operator Kim, please"),
                                       self.config), "owner_mention")
        self.assertEqual(attention_for(message(user=PROTECTED, text="운영자님 확인"),
                                       self.config), "owner_mention")
        self.assertEqual(attention_for(message(user=PROTECTED, text="dummy"), self.config),
                         "always_human")
        self.assertIsNone(attention_for(message(user=PLAIN, text="dummy"), self.config))
        self.assertIsNone(attention_for(message(user=PLAIN, text=None), self.config))
        self.assertFalse(exact_mention(message(text="Operator Kim"), self.config))
        self.assertTrue(exact_mention(message(text=f"<@{OWNER}>"), self.config))

    def test_default_empty_list_matches_nothing(self):
        config = parse_config(raw_config())
        self.assertFalse(name_mention({"text": "Operator Kim 운영자님"}, config))
        self.assertIsNone(attention_for(message(user=PLAIN, text="Operator Kim"), config))


class RouteTests(unittest.TestCase):
    def setUp(self):
        base = parse_config(raw_config())
        self.true_mode = with_names(base)
        self.owner_mode = replace(self.true_mode, mention_only_owner_channels=(MENTION_ONLY,))
        self.lookup = Mock(return_value=None)

    def route(self, config, **changes):
        row = message(**dict({"channel": MENTION_ONLY, "user": PLAIN, "text": "dummy text",
                              "thread_ts": None}, **changes))
        return route(row, config, self.lookup)

    def test_protected_root_naming_the_operator_in_owner_mode(self):
        decision = self.route(self.owner_mode, user=PROTECTED,
                              text="운영자님, 자료 확인 부탁드립니다")
        self.assertEqual((decision.lane, decision.rule, decision.attention),
                         (DEFAULT_LANE, "channel_default", "owner_mention"))
        plain = self.route(self.owner_mode, user=PROTECTED, text="자료 확인 부탁드립니다")
        self.assertEqual((plain.lane, plain.reason, plain.attention), (None, NO_MENTION, None))

    def test_name_and_exact_mention_route_alike(self):
        for config in (self.true_mode, self.owner_mode):
            for user in (PLAIN, PROTECTED):
                with self.subTest(owner_mode=config is self.owner_mode, user=user):
                    named = self.route(config, user=user, text="Operator Kim please check")
                    exact = self.route(config, user=user, text=f"<@{OWNER}> please check")
                    self.assertEqual(named, exact)
                    self.assertEqual(named.attention, "owner_mention")

    def test_ordinary_author_in_true_mode(self):
        decision = self.route(self.true_mode, text="operator kim please check")
        self.assertEqual((decision.lane, decision.rule, decision.attention),
                         (DEFAULT_LANE, "channel_default", "owner_mention"))

    def test_default_empty_list_keeps_exclusion(self):
        config = parse_config(raw_config())
        decision = self.route(config, text="Operator Kim please check")
        self.assertEqual(decision.reason, NO_MENTION)

    def test_bot_posts_are_unchanged(self):
        bot = self.route(self.true_mode, text="Operator Kim", is_bot=True)
        self.assertEqual(bot.reason, "excluded:bot")
        exclude_all = replace(self.true_mode, bot_messages=replace(
            self.true_mode.bot_messages, policy="exclude_all"))
        decision = route(message(channel=NORMAL, user=PLAIN, text="Operator Kim",
                                 thread_ts=None, is_bot=True), exclude_all, self.lookup)
        self.assertEqual((decision.reason, decision.attention), ("excluded:bot", None))


class NameCase(unittest.TestCase):
    followup = "changed_after_ack"
    owner_mode = True

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-owner-name-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        base = fixture_config(self.root)
        config = replace(base, wake_policy=replace(base.wake_policy, followup=self.followup))
        if self.owner_mode:
            config = replace(config, mention_only_owner_channels=(MENTION_ONLY,))
        self.config = self.configure(config)
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id, session_pointer=lane.session, paused=lane.paused)
        self.lane, self.human = Mock(), Mock()
        self.now = 1000.0
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(),
                                 [FileInboxNotifier(self.config.inbox.root, self.store.get_message),
                                  self.lane, self.human],
                                 WakePolicyEngine(self.config.wake_policy, lambda: self.now),
                                 lambda: self.now)

    def configure(self, config):
        return with_names(config)

    def handle(self, number, **changes):
        return self.pipeline.handle_event(event(number, **dict(
            {"channel": MENTION_ONLY, "text": "dummy text"}, **changes)))

    def notices(self, mid):
        return [row["kind"] for row in
                self.store._rows("SELECT kind FROM human_notices WHERE message_id=?", (mid,))]

    def inbox_ids(self):
        ids = []
        for path in sorted((self.root / "inbox").glob("*.jsonl")):
            ids.extend(path.read_text(encoding="utf-8").splitlines())
        return "\n".join(ids)

    def outcome(self, result):
        row = self.store.get_message(result.message_id)
        return dict(result_state=result.state, result_lane=result.lane, state=row["state"],
                    lane=row["lane"], route_reason=row["route_reason"], note=row["note"],
                    attention=row["attention"], followup=row.get("followup"),
                    notices=self.notices(result.message_id),
                    inboxed=result.message_id in self.inbox_ids(),
                    binding=self.store.lookup_binding("slack", row["channel"],
                                                      row.get("thread_ts") or row["ts"]))


class OwnerModePipelineTests(NameCase):
    def test_protected_root_naming_the_operator_is_assigned(self):
        result = self.handle(1, user=PROTECTED, text="운영자님, 자료 확인 부탁드립니다")
        got = self.outcome(result)
        self.assertEqual((got["state"], got["lane"], got["route_reason"], got["attention"]),
                         ("assigned", DEFAULT_LANE, "channel_default", "owner_mention"))
        self.assertTrue(got["inboxed"])
        self.assertEqual(got["notices"], ["attention:owner_mention"])
        self.assertEqual(got["binding"], DEFAULT_LANE)
        self.lane.notify.assert_called()
        counters = self.pipeline.snapshot()
        self.assertEqual(counters["owner_name_mentions"], 1)
        self.assertNotIn("excluded_no_mention", counters)

    def test_same_post_without_the_name_is_excluded(self):
        result = self.handle(1, user=PROTECTED, text="자료 확인 부탁드립니다")
        got = self.outcome(result)
        self.assertEqual((got["state"], got["note"], got["lane"], got["attention"]),
                         ("excluded", NO_MENTION, None, None))
        self.assertEqual(got["notices"], [])
        self.assertFalse(got["inboxed"])
        counters = self.pipeline.snapshot()
        self.assertEqual(counters["excluded_no_mention"], 1)
        self.assertNotIn("owner_name_mentions", counters)

    def test_exact_mention_does_not_count(self):
        result = self.handle(1, user=PROTECTED, text=f"<@{OWNER}> Operator Kim please")
        self.assertEqual(self.outcome(result)["attention"], "owner_mention")
        self.assertNotIn("owner_name_mentions", self.pipeline.snapshot())

    def test_name_and_exact_mention_have_identical_outcomes(self):
        named = self.outcome(self.handle(1, user=PROTECTED, text="Operator Kim please check"))
        exact = self.outcome(self.handle(2, user=PROTECTED, text=f"<@{OWNER}> please check"))
        self.assertEqual(named, exact)
        self.assertEqual(self.pipeline.snapshot()["owner_name_mentions"], 1)

    def test_reply_in_the_bound_thread_keeps_working(self):
        root = self.handle(1, user=PROTECTED, text="Operator Kim please check")
        ts = self.store.get_message(root.message_id)["ts"]
        reply = self.handle(2, thread_ts=ts, is_thread_reply=True, text="dummy follow up")
        got = self.outcome(reply)
        self.assertEqual((got["state"], got["lane"], got["route_reason"], got["attention"]),
                         ("assigned", DEFAULT_LANE, "thread_inherit", None))
        self.assertTrue(got["inboxed"])
        self.assertEqual(self.pipeline.snapshot()["owner_name_mentions"], 1)

    def test_operator_post_naming_themselves_is_an_owner_post(self):
        result = self.handle(1, user=OWNER, text="Operator Kim here, dummy note 운영자님")
        got = self.outcome(result)
        self.assertEqual((got["state"], got["note"], got["route_reason"], got["attention"]),
                         ("done", "owner_post", "owner_post", None))
        self.assertEqual(got["notices"], [])
        self.assertFalse(got["inboxed"])
        self.assertNotIn("owner_name_mentions", self.pipeline.snapshot())

    def test_bot_post_naming_the_operator_is_excluded(self):
        result = self.handle(1, text="Operator Kim", is_bot=True)
        got = self.outcome(result)
        self.assertEqual((got["state"], got["attention"]), ("excluded", None))
        self.assertNotIn("owner_name_mentions", self.pipeline.snapshot())


class NoticeFlagTests(NameCase):
    """The P29 assigned notice flags treat a name like an exact mention."""

    def configure(self, config):
        return with_names(replace(config, human_queue=replace(
            config.human_queue, assigned_notice_flags=("always_human",))))

    def test_owner_mention_notice_is_skipped_for_both(self):
        named = self.outcome(self.handle(1, text="Operator Kim please"))
        exact = self.outcome(self.handle(2, text=f"<@{OWNER}> please"))
        self.assertEqual(named, exact)
        self.assertEqual(named["notices"], [])
        self.assertEqual(self.pipeline.snapshot()["attention_notice_skipped"], 2)


class TrueModePipelineTests(NameCase):
    owner_mode = False

    def test_ordinary_author_naming_the_operator_is_assigned(self):
        result = self.handle(1, user=PLAIN, text="operator kim, a dummy question")
        got = self.outcome(result)
        self.assertEqual((got["state"], got["lane"], got["attention"]),
                         ("assigned", DEFAULT_LANE, "owner_mention"))
        self.assertEqual(got["notices"], ["attention:owner_mention"])
        self.assertEqual(self.pipeline.snapshot()["owner_name_mentions"], 1)

    def test_normal_channel_sets_attention_too(self):
        result = self.handle(1, channel=NORMAL, text="Operator Kim dummy")
        got = self.outcome(result)
        self.assertEqual((got["state"], got["lane"], got["attention"]),
                         ("assigned", "support", "owner_mention"))


class DefaultEmptyPipelineTests(NameCase):
    owner_mode = False

    def configure(self, config):
        return config

    def test_nothing_changes(self):
        self.assertEqual(self.config.always_human.mention_texts, ())
        result = self.handle(1, text="Operator Kim 운영자님 please")
        got = self.outcome(result)
        self.assertEqual((got["state"], got["note"], got["attention"]),
                         ("excluded", NO_MENTION, None))
        other = self.outcome(self.handle(2, channel=NORMAL, text="Operator Kim"))
        self.assertEqual((other["state"], other["attention"]), ("assigned", None))
        self.assertNotIn("owner_name_mentions", self.pipeline.snapshot())


class EditParityTests(NameCase):
    """An edit that adds the name to an excluded:no_mention post does what an edit adding
    an exact mention does (P36 keeps the P22/P24 edit semantics; since P37 both are routed
    as a first assignment under every wake_policy.followup value)."""

    def edit_both(self):
        first = self.handle(1, user=PROTECTED, text="dummy request")
        second = self.handle(2, user=PROTECTED, text="dummy request")
        self.assertEqual((first.state, second.state), ("excluded", "excluded"))
        self.now += 5
        named = self.handle(3, ts="1000.000001", user=PROTECTED, subtype="message_changed",
                            text="Operator Kim dummy request")
        exact = self.handle(4, ts="1000.000002", user=PROTECTED, subtype="message_changed",
                            text=f"<@{OWNER}> dummy request")
        self.assertEqual((named.message_id, exact.message_id),
                         (first.message_id, second.message_id))
        return self.outcome(named), self.outcome(exact)

    def test_edit_outcome(self):
        """Under changed_after_ack (the default) both edits are assigned to the default lane
        as a first assignment (P37; before P37 they were recorded only)."""
        named, exact = self.edit_both()
        self.assertEqual(named, exact)
        self.assertEqual((named["state"], named["lane"], named["attention"], named["inboxed"]),
                         ("assigned", DEFAULT_LANE, "owner_mention", True))
        self.assertEqual(named["notices"], ["attention:owner_mention"])
        counters = self.pipeline.snapshot()
        self.assertEqual((counters["edits_mention_added"], counters["owner_name_mentions"]),
                         (2, 1))
        self.assertNotIn("edits_ignored", counters)


class EditParityAlwaysTests(EditParityTests):
    followup = "always"

    def test_edit_outcome(self):
        """Under always both edits are routed again and assigned to the default lane (since
        P37 as a first assignment, outside the edit wake counters)."""
        named, exact = self.edit_both()
        self.assertEqual(named, exact)
        self.assertEqual((named["state"], named["lane"], named["attention"], named["inboxed"]),
                         ("assigned", DEFAULT_LANE, "owner_mention", True))
        self.assertEqual(named["notices"], ["attention:owner_mention"])
        counters = self.pipeline.snapshot()
        self.assertEqual((counters["edits_mention_added"], counters["owner_name_mentions"]),
                         (2, 1))
        self.assertNotIn("edits_woken", counters)


class WakeEngineTests(unittest.TestCase):
    """fix1: attention `owner_mention` bypasses the same-author debounce."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-owner-name-wake-")
        self.addCleanup(temp.cleanup)
        self.now = 0
        self.engine = WakePolicyEngine({"enabled": True, "debounce_seconds": 90,
                                        "storm_per_hour": 30, "batch_seconds": 600,
                                        "state_dir": temp.name, "immediate_users": [],
                                        "owner_user": "UDUMMY_OWNER"}, lambda: self.now)

    def offer(self, mid, **extra):
        return self.engine.offer("support", dict(id=mid, **extra), "UDUMMY")

    def test_owner_mention_attention_bypasses_the_debounce(self):
        self.assertIsInstance(self.offer("m1"), Deliver)
        self.now = 1
        self.assertIsInstance(self.offer("m2", attention="owner_mention"), Deliver)
        self.assertIsInstance(self.offer("m3"), Defer)
        self.assertIsInstance(self.offer("m4", attention="always_human"), Defer)
        self.assertIsInstance(self.offer("m5", attention=None), Defer)
        self.assertIsInstance(self.offer("m6", text="<@UDUMMY_OWNER> dummy"), Deliver)

    def test_storm_guard_still_applies(self):
        for number in range(29):
            self.offer(f"m{number}")
        self.assertIsInstance(self.offer("m9", attention="owner_mention"), Batch)


class RecordingWake(WakePolicyEngine):
    def __init__(self, *args):
        super().__init__(*args)
        self.results = []

    def offer(self, lane, message, author):
        result = super().offer(lane, message, author)
        self.results.append((message.get("id"), type(result).__name__))
        return result


class PipelineWakeTests(NameCase):
    """fix1: in the pipeline a name post within the same author's debounce window wakes the
    lane at once; a plain post is still debounced; an exact mention behaves as before."""
    owner_mode = False

    def setUp(self):
        super().setUp()
        self.wake = RecordingWake(self.config.wake_policy, lambda: self.now)
        self.pipeline.wake_engine = self.wake

    def step(self, number, **changes):
        self.now += 5
        result = self.handle(number, channel=NORMAL, **changes)
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        return self.wake.results[-1][1]

    def test_name_post_wakes_within_the_debounce_window(self):
        self.assertEqual(self.step(1, text="dummy one"), "Deliver")
        self.assertEqual(self.step(2, text="Operator Kim, dummy two"), "Deliver")
        self.assertEqual(self.step(3, text="dummy three"), "Defer")
        self.assertEqual(self.step(4, text="운영자님 dummy four"), "Deliver")

    def test_exact_mentions_behave_as_before(self):
        wake_owner = self.config.wake_policy.owner_user
        self.assertEqual(self.step(1, text="dummy one"), "Deliver")
        self.assertEqual(self.step(2, text=f"<@{wake_owner}> dummy"), "Deliver")
        self.assertEqual(self.step(3, text=f"<@{OWNER}> dummy"), "Deliver")
        self.assertEqual(self.step(4, text="dummy four"), "Defer")
        self.assertNotIn("owner_name_mentions", self.pipeline.snapshot())

    def test_other_attention_is_still_debounced(self):
        self.assertEqual(self.step(1, user=PROTECTED, text="dummy one"), "Deliver")
        self.assertEqual(self.step(2, user=PROTECTED, text="dummy two"), "Defer")
        self.assertEqual(self.store.get_message("slack:C0000000001:1000.000002")["attention"],
                         "always_human")


if __name__ == "__main__":
    unittest.main()
