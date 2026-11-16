"""P16 bot flag and user directory: a post by a human is a human post."""
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.config.schema import UserDirectory as UserDirectoryConfig
from slack_dispatch.ingest.slack_reconcile import READ_METHODS, SlackReconcileSource
from slack_dispatch.ingest.user_directory import UserDirectory, is_bot_message, parse_user
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.engine import route
from slack_dispatch.service import build_service_sources, build_user_directory
from tests.test_pipeline_integration import SAMPLE, event, fixture_config
from tests.test_router_config import raw_config

HUMAN = "U0000000077"
BOT = "U0000000081"
OWNER = "U0000000093"
DEFAULT_CHANNEL = "C0000000001"   # channel default lane: support
NO_DEFAULT_CHANNEL = "C0000000002"  # channel default: null


class SlackApiError(Exception):
    def __init__(self, error):
        super().__init__("dummy failure text")
        self.response = SimpleNamespace(data={"ok": False, "error": error})


class FakeSlack:
    """users.info answers keyed by user id; an Exception value is raised."""

    def __init__(self, answers=None):
        self.answers = {
            BOT: {"ok": True, "user": {"id": BOT, "is_bot": True, "name": "dummy-bot"}},
            HUMAN: {"ok": True, "user": {"id": HUMAN, "is_bot": False, "name": "dummy-human"}},
        }
        self.answers.update(answers or {})
        self.calls = []

    def api_call(self, method, http_verb="POST", params=None):
        self.calls.append((method, http_verb, dict(params or {})))
        answer = self.answers[params["user"]]
        if isinstance(answer, Exception):
            raise answer
        return answer


def config_with(**bots):
    cfg = parse_config(raw_config())
    return replace(cfg, bot_messages=replace(cfg.bot_messages, **bots)) if bots else cfg


class RuleTests(unittest.TestCase):
    def test_truth_table(self):
        cfg = config_with(known_bot_users=(BOT,))
        directory = Mock()
        directory.is_bot.side_effect = lambda user: user == "U0000000082"
        cases = [
            ({"user": None}, None, True),                                       # (a)
            ({"user": None, "author": HUMAN}, None, True),                       # (a) user key wins
            ({"author": None}, None, True),                                     # (a) ledger row
            ({"user": HUMAN, "subtype": "bot_message"}, None, True),            # (b)
            ({"user": BOT}, None, True),                                        # (c)
            ({"author": BOT}, None, True),                                      # (c) ledger row
            ({"user": "U0000000082", "bot_id": "B0000000001"}, directory, True),  # (d)
            ({"user": "U0000000082", "bot_id": "B0000000001"}, None, False),  # no directory
            ({"user": HUMAN, "bot_id": "B0000000001"}, None, False),           # bot_id alone
            ({"user": HUMAN, "app_id": "A0000000001"}, None, False),           # app_id alone
            ({"user": HUMAN, "bot_profile": {"name": "dummy"}}, None, False),  # bot_profile alone
            ({"user": HUMAN, "bot_id": "B0000000001"}, directory, False),      # directory: human
            ({"user": HUMAN}, None, False),
        ]
        for message, given, expected in cases:
            with self.subTest(message=message, directory=given is not None):
                self.assertIs(is_bot_message(message, cfg, given), expected)

    def test_config_none_has_no_known_bots(self):
        self.assertFalse(is_bot_message({"user": BOT}, None))
        self.assertTrue(is_bot_message({"user": None}, None))

    def test_users_info_is_on_the_read_allowlist(self):
        self.assertIn("users.info", READ_METHODS)

    def test_parse_user(self):
        self.assertEqual(parse_user({"user": {"id": BOT, "is_bot": True, "is_app_user": True,
                                              "deleted": False, "name": "dummy"}}),
                         dict(user=BOT, is_bot=1, is_app_user=1, deleted=0, name="dummy"))
        for response in ({}, {"user": "x"}, {"user": {"id": "bad id"}}, None):
            self.assertIsNone(parse_user(response))


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.store = LedgerStore(":memory:")
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.now = 1_000_000.0
        self.client = FakeSlack()
        self.directory = UserDirectory(self.client, self.store, 86400, (BOT + "9",),
                                       clock=lambda: self.now)

    def test_bot_and_human_are_cached_in_the_ledger(self):
        self.assertTrue(self.directory.is_bot(BOT))
        self.assertFalse(self.directory.is_bot(HUMAN))
        self.assertTrue(self.directory.is_bot(BOT))
        self.assertEqual([call[:2] for call in self.client.calls], [("users.info", "GET")] * 2)
        self.assertEqual(self.client.calls[0][2], {"user": BOT})
        row = self.store.get_user_directory(BOT)
        self.assertEqual((row["is_bot"], row["is_app_user"], row["deleted"], row["name"]),
                         (1, 0, 0, "dummy-bot"))
        self.assertEqual(self.directory.snapshot(), {"directory_lookups": 2, "directory_failures": 0})
        # A new process reads the table without calling Slack.
        fresh = UserDirectory(self.client, self.store, clock=lambda: self.now)
        self.assertTrue(fresh.is_bot(BOT))
        self.assertEqual(len(self.client.calls), 2)

    def test_known_bot_users_never_call(self):
        self.assertTrue(self.directory.is_bot(BOT + "9"))
        self.assertTrue(self.directory.is_bot(None))
        self.assertEqual(self.client.calls, [])

    def test_ttl_refresh(self):
        self.assertFalse(self.directory.is_bot(HUMAN))
        self.now += 86399
        self.directory.is_bot(HUMAN)
        self.assertEqual(len(self.client.calls), 1)
        self.client.answers[HUMAN] = {"ok": True, "user": {"id": HUMAN, "is_bot": True}}
        self.now += 1
        self.assertTrue(self.directory.is_bot(HUMAN))
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.store.get_user_directory(HUMAN)["is_bot"], 1)

    def test_failures_fail_open_without_cache_and_warn_once_per_hour(self):
        failures = {"U0000000061": SlackApiError("ratelimited"),
                    "U0000000062": {"ok": False, "error": "user_not_found"},
                    "U0000000063": {"ok": True},
                    "U0000000064": {"ok": True, "user": {"id": "U0000000065", "is_bot": True}}}
        self.client.answers.update(failures)
        for user in failures:
            with self.subTest(user=user):
                with self.assertLogs("slack_dispatch.ingest.user_directory", "WARNING") as logs:
                    self.assertFalse(self.directory.is_bot(user))
                self.assertEqual(len(logs.output), 1)
                self.assertNotIn("dummy failure text", logs.output[0])
                self.assertIsNone(self.store.get_user_directory(user))
        logger = "slack_dispatch.ingest.user_directory"
        self.now += 3599  # past the cooldown, still within the warning hour
        with self.assertNoLogs(logger, "WARNING"):
            self.assertFalse(self.directory.is_bot("U0000000061"))
        self.now += 300  # past the next cooldown and the warning hour
        with self.assertLogs(logger, "WARNING") as logs:
            self.directory.is_bot("U0000000061")
        self.assertIn("class=SlackApiError error=ratelimited", logs.output[0])
        self.assertEqual(self.directory.snapshot(), {"directory_lookups": 6, "directory_failures": 6})

    def test_expired_row_is_kept_when_refresh_fails(self):
        self.assertTrue(self.directory.is_bot(BOT))
        self.assertFalse(self.directory.is_bot(HUMAN))
        self.now += 86400
        self.client.answers[BOT] = SlackApiError("ratelimited")
        self.client.answers[HUMAN] = {"ok": False, "error": "internal_error"}
        with self.assertLogs("slack_dispatch.ingest.user_directory", "WARNING"):
            self.assertTrue(self.directory.is_bot(BOT))
            self.assertFalse(self.directory.is_bot(HUMAN))
        # The expired rows are not rewritten by a failure.
        self.assertEqual(self.store.get_user_directory(BOT)["is_bot"], 1)
        self.assertEqual(self.directory.snapshot(), {"directory_lookups": 4, "directory_failures": 2})

    def test_failure_cooldown_skips_lookups(self):
        self.client.answers[HUMAN] = SlackApiError("ratelimited")
        with self.assertLogs("slack_dispatch.ingest.user_directory", "WARNING"):
            self.assertFalse(self.directory.is_bot(HUMAN))
        self.now += 299
        self.assertFalse(self.directory.is_bot(HUMAN))
        self.assertEqual(len(self.client.calls), 1)
        self.client.answers[HUMAN] = {"ok": True, "user": {"id": HUMAN, "is_bot": True}}
        self.now += 1
        self.assertTrue(self.directory.is_bot(HUMAN))
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.directory.snapshot(), {"directory_lookups": 2, "directory_failures": 1})
        # A success clears the cooldown: the next expiry refreshes at once.
        self.assertEqual(self.directory._failed_at, {})

    def test_cooldown_uses_the_cached_row(self):
        self.assertTrue(self.directory.is_bot(BOT))
        self.now += 86400
        self.client.answers[BOT] = {"ok": False, "error": "ratelimited"}
        with self.assertLogs("slack_dispatch.ingest.user_directory", "WARNING"):
            self.assertTrue(self.directory.is_bot(BOT))
        self.now += 100
        self.assertTrue(self.directory.is_bot(BOT))
        self.assertEqual(len(self.client.calls), 2)

    def test_custom_cooldown(self):
        directory = UserDirectory(self.client, self.store, clock=lambda: self.now,
                                  failure_cooldown_seconds=10)
        self.client.answers[HUMAN] = SlackApiError("ratelimited")
        with self.assertLogs("slack_dispatch.ingest.user_directory", "WARNING"):
            directory.is_bot(HUMAN)
        self.now += 9
        directory.is_bot(HUMAN)
        self.now += 1
        directory.is_bot(HUMAN)
        self.assertEqual(len(self.client.calls), 2)

    def test_no_client_fails_open(self):
        directory = UserDirectory(lambda: None, self.store, clock=lambda: self.now)
        with self.assertLogs("slack_dispatch.ingest.user_directory", "WARNING"):
            self.assertFalse(directory.is_bot(HUMAN))
        self.assertEqual(directory.snapshot(), {"directory_lookups": 1, "directory_failures": 1})

    def test_callable_client(self):
        directory = UserDirectory(lambda: self.client, self.store, clock=lambda: self.now)
        self.assertTrue(directory.is_bot(BOT))


class RouterTests(unittest.TestCase):
    def test_router_respects_precomputed_flag_and_never_calls_directory(self):
        cfg = config_with(known_bot_users=(BOT,))
        with patch.object(UserDirectory, "is_bot", side_effect=AssertionError("router called Slack")):
            # Precomputed human flag wins over the known bot list and bot_id.
            decision = route(dict(event(user=BOT, bot_id="B0000000001", channel=NO_DEFAULT_CHANNEL,
                                        text="plain"), is_bot=False), cfg, Mock(return_value=None))
            self.assertEqual(decision.rule, "none")
            # A precomputed bot flag excludes a human author.
            decision = route(dict(event(user=HUMAN, channel=NO_DEFAULT_CHANNEL), is_bot=True),
                             cfg, Mock(return_value=None))
            self.assertEqual(decision.reason, "excluded:bot")
            # Ledger rows carry is_bot as an integer.
            row = dict(author=HUMAN, channel=NO_DEFAULT_CHANNEL, ts="1.0", source="slack",
                       is_bot=1, text="plain")
            self.assertEqual(route(row, cfg, Mock(return_value=None)).reason, "excluded:bot")
            self.assertEqual(route(dict(row, is_bot=0), cfg, Mock(return_value=None)).rule, "none")
            # Without the key the helper runs without a directory: bot_id alone is human.
            message = event(user=HUMAN, bot_id="B0000000001", channel=NO_DEFAULT_CHANNEL,
                            text="<@U0000000093>")
            decision = route(message, cfg, Mock(return_value=None))
            self.assertEqual((decision.rule, decision.attention), ("always_human", "owner_mention"))
            self.assertEqual(route(event(user=BOT), cfg, Mock()).reason, "excluded:bot")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p16-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = fixture_config(self.root)
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id, session_pointer="dummy-session", paused=False)
        self.now = 1000.0
        self.clock = lambda: self.now
        self.client = FakeSlack()
        self.make_pipeline()

    def make_pipeline(self, directory=True):
        self.receivers = [Mock(), Mock(), Mock()]
        self.suggester = Mock(enabled=True)
        self.suggester.suggest.return_value = None
        self.pipeline = Pipeline(self.config, self.store, self.suggester, self.receivers,
                                 WakePolicyEngine(self.config.wake_policy, self.clock), self.clock)
        if directory:
            self.pipeline.directory = UserDirectory(self.client, self.store,
                                                    known_bot_users=self.config.bot_messages.known_bot_users,
                                                    clock=self.clock)

    def app_post(self, number, user=HUMAN, channel=DEFAULT_CHANNEL, text="<@U0000000093> please check",
                 **changes):
        return event(number, user=user, channel=channel, text=text, bot_id="B0000000077", **changes)

    def test_human_app_post_with_owner_mention_is_assigned_with_immediate_notice(self):
        result = self.pipeline.handle_event(self.app_post(1))
        row = self.store.get_message(result.message_id)
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        self.assertEqual((row["attention"], row["is_bot"]), ("owner_mention", 0))
        self.receivers[-1].notify.assert_called_once_with("human_queue", [result.message_id])
        self.assertEqual(self.client.calls[0][2], {"user": HUMAN})

    def test_human_app_post_without_default_lane_becomes_human_pending(self):
        result = self.pipeline.handle_event(self.app_post(1, channel=NO_DEFAULT_CHANNEL))
        row = self.store.get_message(result.message_id)
        self.assertEqual(result.state, "human_pending")
        self.assertNotEqual(row["note"], "excluded:bot")
        self.assertEqual(row["attention"], "owner_mention")
        self.receivers[-1].notify.assert_called_once_with("human_queue", [result.message_id])

    def test_bot_user_post_keeps_route_or_exclude(self):
        excluded = self.pipeline.handle_event(self.app_post(1, user=BOT, channel=NO_DEFAULT_CHANNEL))
        self.assertEqual(excluded.state, "excluded")
        self.assertEqual(self.store.get_message(excluded.message_id)["note"], "excluded:bot")
        self.assertEqual(self.store.get_message(excluded.message_id)["is_bot"], 1)
        assigned = self.pipeline.handle_event(self.app_post(2, user=BOT, text="[content] <@U0000000093>"))
        self.assertEqual((assigned.state, assigned.lane), ("assigned", "support"))
        self.assertIsNone(self.store.get_message(assigned.message_id)["attention"])
        self.pipeline.flush(5000)
        self.receivers[-1].notify.assert_not_called()
        self.suggester.suggest.assert_not_called()
        self.assertEqual(len(self.client.calls), 1)  # cached for the second post

    def test_hand_posted_human_never_costs_a_lookup(self):
        self.pipeline.handle_event(event(1, user=HUMAN, channel=NO_DEFAULT_CHANNEL, text="plain"))
        self.pipeline.handle_event(event(2, user=HUMAN, channel=NO_DEFAULT_CHANNEL, text="plain",
                                         event_type="reaction_added", reaction="eyes"))
        self.assertEqual(self.client.calls, [])
        self.pipeline.handle_event(event(3, user=HUMAN, app_id="A0000000001", text="plain"))
        self.assertEqual(len(self.client.calls), 1)

    def test_directory_disabled_uses_rules_a_b_c_only(self):
        self.make_pipeline(directory=False)
        human = self.pipeline.handle_event(self.app_post(1, user=BOT, channel=NO_DEFAULT_CHANNEL))
        self.assertEqual(human.state, "human_pending")
        self.assertEqual(self.client.calls, [])
        self.config = replace(self.config, bot_messages=replace(self.config.bot_messages,
                                                                known_bot_users=(BOT,)))
        self.make_pipeline(directory=False)
        bot = self.pipeline.handle_event(self.app_post(2, user=BOT, channel=NO_DEFAULT_CHANNEL))
        self.assertEqual(self.store.get_message(bot.message_id)["note"], "excluded:bot")
        bot = self.pipeline.handle_event(event(3, user=HUMAN, subtype="bot_message",
                                               channel=NO_DEFAULT_CHANNEL))
        self.assertEqual(self.store.get_message(bot.message_id)["note"], "excluded:bot")

    def test_lookup_failure_fails_open_and_warns_once(self):
        self.client.answers[HUMAN] = SlackApiError("internal_error")
        with self.assertLogs("slack_dispatch.ingest.user_directory", "WARNING") as logs:
            first = self.pipeline.handle_event(self.app_post(1, channel=NO_DEFAULT_CHANNEL))
            second = self.pipeline.handle_event(self.app_post(2, channel=NO_DEFAULT_CHANNEL))
        self.assertEqual(len(logs.output), 1)
        self.assertEqual((first.state, second.state), ("human_pending", "human_pending"))
        self.assertIsNone(self.store.get_user_directory(HUMAN))
        # The second post falls in the failure cooldown: no second users.info call.
        self.assertEqual(self.pipeline.snapshot()["directory_failures"], 1)
        self.assertEqual(self.pipeline.snapshot()["directory_lookups"], 1)

    def test_heartbeat_counters(self):
        self.pipeline.handle_event(self.app_post(1))                                # human via app
        self.pipeline.handle_event(self.app_post(2, user=BOT))                       # bot user
        self.pipeline.handle_event(event(3, user=None, channel=NO_DEFAULT_CHANNEL))  # no author
        self.pipeline.handle_event(event(4, user=HUMAN, text="plain"))               # by hand
        self.pipeline.handle_event(self.app_post(1))                                # duplicate
        counters = self.pipeline.snapshot()
        self.assertEqual({key: counters[key] for key in
                          ("directory_lookups", "directory_failures", "app_posts_human", "bot_posts")},
                         {"directory_lookups": 2, "directory_failures": 0, "app_posts_human": 1,
                          "bot_posts": 2})
        self.make_pipeline(directory=False)
        self.assertFalse({"directory_lookups", "app_posts_human", "bot_posts"} & set(self.pipeline.snapshot()))

    def test_caller_supplied_bot_flag_is_kept(self):
        result = self.pipeline.handle_event(event(1, user=HUMAN, is_bot=True, channel=NO_DEFAULT_CHANNEL))
        self.assertEqual(self.store.get_message(result.message_id)["note"], "excluded:bot")
        self.assertEqual(self.client.calls, [])

    def test_socket_and_reconcile_events_get_the_flag_in_the_pipeline(self):
        cfg = replace(self.config, sources=load_config(SAMPLE).sources)
        socket, reconcile = build_service_sources(cfg, self.pipeline.handle_event, self.store)
        socket._response_class = lambda **kwargs: kwargs
        request = SimpleNamespace(envelope_id="dummy", type="events_api", payload={
            "event_id": "EvDUMMY91", "event": dict(type="message", channel=NO_DEFAULT_CHANNEL,
                                                    ts="2000.000001", user=HUMAN, bot_id="B0000000077",
                                                    text="<@U0000000093> socket")})
        socket._receive(Mock(), request)
        socket_row = self.store.get_message(f"slack:{NO_DEFAULT_CHANNEL}:2000.000001")
        self.assertEqual((socket_row["is_bot"], socket_row["state"]), (0, "human_pending"))
        self.store.set_cursor("slack", NO_DEFAULT_CHANNEL, "1")
        reconcile.client = Mock()
        reconcile.client.api_call.return_value = {"messages": [
            dict(ts="2000.000002", user=BOT, bot_id="B0000000081", text="bot report"),
            dict(ts="2000.000003", user=HUMAN, bot_id="B0000000077", text="plain")]}
        reconcile.catch_up()
        bot_row = self.store.get_message(f"slack:{NO_DEFAULT_CHANNEL}:2000.000002")
        human_row = self.store.get_message(f"slack:{NO_DEFAULT_CHANNEL}:2000.000003")
        self.assertEqual((bot_row["is_bot"], bot_row["note"]), (1, "excluded:bot"))
        self.assertEqual((human_row["is_bot"], human_row["state"]), (0, "human_pending"))
        # One users.info call per distinct user, all through the directory client.
        self.assertEqual(sorted(call[2]["user"] for call in self.client.calls), [HUMAN, BOT])
        self.assertFalse(any(call.args[0] == "users.info"
                             for call in reconcile.client.api_call.call_args_list))

    def test_edit_keeps_the_pipeline_flag(self):
        self.pipeline.handle_event(self.app_post(1, channel=NO_DEFAULT_CHANNEL))
        self.pipeline.handle_event(self.app_post(2, channel=NO_DEFAULT_CHANNEL, ts="1000.000001",
                                                 subtype="message_changed"))
        self.assertEqual(self.store.get_message(f"slack:{NO_DEFAULT_CHANNEL}:1000.000001")["is_bot"], 0)


class ConfigAndServiceTests(unittest.TestCase):
    def test_defaults_sample_and_invalid_values(self):
        self.assertEqual(parse_config(raw_config()).user_directory, UserDirectoryConfig())
        self.assertEqual(UserDirectoryConfig().failure_cooldown_seconds, 300)
        self.assertEqual(load_config(SAMPLE).user_directory, UserDirectoryConfig(True, 86400, 300))
        raw = raw_config()
        raw["user_directory"] = {"enabled": False, "ttl_seconds": 60, "failure_cooldown_seconds": 30}
        self.assertEqual(parse_config(raw).user_directory, UserDirectoryConfig(False, 60, 30))
        for value in ({"enabled": "yes"}, {"ttl_seconds": 0}, {"ttl_seconds": True}, "on",
                      {"failure_cooldown_seconds": 0}, {"failure_cooldown_seconds": "300"}):
            raw = raw_config()
            raw["user_directory"] = value
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_config(raw)

    def test_build_user_directory(self):
        store = LedgerStore(":memory:")
        cfg = replace(load_config(SAMPLE), bot_messages=replace(load_config(SAMPLE).bot_messages,
                                                                known_bot_users=(BOT,)))
        sources = build_service_sources(cfg, Mock(), store)
        directory = build_user_directory(cfg, store, sources, lambda: 0)
        self.assertIsInstance(directory, UserDirectory)
        self.assertEqual(directory.known_bot_users, {BOT})
        self.assertEqual(directory.failure_cooldown_seconds, 300)
        self.assertIsNone(build_user_directory(cfg, store, [], lambda: 0))
        disabled = replace(cfg, user_directory=UserDirectoryConfig(enabled=False))
        self.assertIsNone(build_user_directory(disabled, store, sources, lambda: 0))
        reconcile = next(source for source in sources if isinstance(source, SlackReconcileSource))
        reconcile.client = FakeSlack()
        store.open()
        self.addCleanup(store.close)
        store.migrate()
        self.assertTrue(directory.is_bot("U0000000081"))


if __name__ == "__main__":
    unittest.main()
