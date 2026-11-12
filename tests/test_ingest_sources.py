import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.config.schema import SourceConfig
from slack_dispatch.ingest import build_sources
from slack_dispatch.ingest.normalize import normalize_slack_event
from slack_dispatch.ingest.slack_reconcile import SlackReconcileSource
from slack_dispatch.ingest.slack_socket import SlackSocketSource
from slack_dispatch.ingest.tokens import TokenError, parse_env
from slack_dispatch.ledger.store import LedgerStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / "tests/fixtures/slack_events/events.json").read_text(encoding="utf-8"))
STAMP = "2026-01-01T00:00:00+00:00"


class NormalizeTests(unittest.TestCase):
    def test_all_supported_events(self):
        for payload in FIXTURES:
            with self.subTest(kind=payload["event"]["type"], subtype=payload["event"].get("subtype")):
                event = normalize_slack_event(payload, STAMP)
                self.assertEqual(event["event_id"], payload["event_id"])
                self.assertEqual(event["channel"], "C0000000001")
                self.assertEqual(event["received_at"], STAMP)
                self.assertNotIn("text", event)
                self.assertNotIn("synthetic", json.dumps(event))
                if event["event_type"] == "message":
                    self.assertEqual(event["ts"], "100.000001")
                if event["subtype"] in ("message_changed", "thread_broadcast"):
                    self.assertTrue(event["is_thread_reply"])
        self.assertEqual(normalize_slack_event(FIXTURES[7], STAMP)["user"], "U0000000001")
        self.assertEqual(normalize_slack_event(FIXTURES[4], STAMP)["reaction"], "eyes")
        self.assertTrue(normalize_slack_event(FIXTURES[6], STAMP)["has_files"])

    def test_opt_in_and_malformed(self):
        self.assertEqual(normalize_slack_event(FIXTURES[0], STAMP, store_body=True)["text"], "synthetic body")
        self.assertNotIn("text", normalize_slack_event(FIXTURES[2], STAMP, store_body=True))
        bad = dict(event_id="synthetic body", event_time=True, retry_attempt=-1,
                   event=dict(type="message", channel="synthetic body", user=[], ts="bad",
                              thread_ts={}, subtype="synthetic body", channel_type="other",
                              text="synthetic body"))
        event = normalize_slack_event(bad, STAMP)
        for key in ("event_id", "event_time", "retry_attempt", "channel", "user", "ts",
                    "thread_ts", "subtype", "channel_type"):
            self.assertIsNone(event[key])
        for payload in (None, {}, {"event": []}, {"event": {"type": []}}, {"event": {"type": "app_mention"}}):
            self.assertIsNone(normalize_slack_event(payload, STAMP))


class ConfigAndTokenTests(unittest.TestCase):
    def test_config_factory_no_io(self):
        config = load_config(ROOT / "examples/instance-sample/config.yaml")
        self.assertFalse(config.ledger.store_body)
        store = LedgerStore(":memory:")
        sources = build_sources(config, lambda event: None, store)
        self.assertEqual([type(source) for source in sources], [SlackSocketSource, SlackReconcileSource])
        self.assertTrue(all(source.client is None for source in sources))
        data = yaml.safe_load((ROOT / "examples/instance-sample/config.yaml").read_text(encoding="utf-8"))
        data["ledger"]["store_body"] = True
        data["sources"][0]["enabled"] = False
        config = parse_config(data)
        self.assertEqual(len(build_sources(config, lambda event: None, store)), 1)
        self.assertTrue(store.store_body)
        for key in ("interval_seconds", "lookback_days"):
            bad = yaml.safe_load((ROOT / "examples/instance-sample/config.yaml").read_text(encoding="utf-8"))
            bad["sources"][1][key] = False
            with self.assertRaises(ConfigError):
                parse_config(bad)

    def test_token_parser_only_synthetic_strings(self):
        self.assertEqual(parse_env("# comment\r\nOTHER=ignored\r\nAPP = xapp-FAKE-example # tail\r\n", "APP", "xapp-"), "xapp-FAKE-example")
        self.assertEqual(parse_env("USER=xoxp-FAKE-example", "USER", "xoxp-"), "xoxp-FAKE-example")
        for text in ("APP=invalid", "OTHER=xapp-FAKE-example"):
            with self.assertRaises(TokenError) as caught:
                parse_env(text, "APP", "xapp-")
            self.assertNotIn("invalid", str(caught.exception))


def sdk_modules(socket_class=None, web_class=None):
    sdk = ModuleType("slack_sdk")
    socket = ModuleType("slack_sdk.socket_mode")
    response = ModuleType("slack_sdk.socket_mode.response")
    sdk.WebClient = web_class
    socket.SocketModeClient = socket_class
    response.SocketModeResponse = lambda **kwargs: SimpleNamespace(**kwargs)
    return {"slack_sdk": sdk, "slack_sdk.socket_mode": socket,
            "slack_sdk.socket_mode.response": response}


class SocketTests(unittest.TestCase):
    def test_observed_disconnect_recovery_disconnect(self):
        source = SlackSocketSource(SourceConfig("slack_socket", True, 3600),
                                   lambda event: None, "dummy.env", "APP")
        for connected, expected in ((False, 0), (True, 0), (False, 1),
                                    (False, 1), (True, 1), (True, 1),
                                    (False, 2), (False, 2)):
            source._observe(connected)
            self.assertEqual(source.disconnects, expected)

    def test_ack_order_disconnects_and_close(self):
        calls, events, options = [], [], []

        class FakeSocket:
            def __init__(self, **kwargs):
                options.append(kwargs)
                self.socket_mode_request_listeners = []

            def connect(self):
                calls.append("connect")

            def is_connected(self):
                return True

            def send_socket_mode_response(self, response):
                calls.append("ack")

            def close(self):
                calls.append("close")

        def receive(event):
            calls.append("handle")
            events.append(event)

        source = SlackSocketSource(SourceConfig("slack_socket", True, 3600), receive,
                                   "dummy.env", "APP")
        with patch.dict(sys.modules, sdk_modules(socket_class=FakeSocket, web_class=lambda **kwargs: SimpleNamespace(**kwargs))), patch(
                "slack_dispatch.ingest.slack_socket.load_token", return_value="xapp-FAKE-example"):
            source.start()
            self.addCleanup(source.stop)
            client = source.client
            request = SimpleNamespace(envelope_id="dummy-envelope", type="events_api",
                                      payload=FIXTURES[0], retry_attempt=2)
            source._receive(client, request)
            self.assertEqual(calls[-2:], ["ack", "handle"])
            self.assertNotIn("text", events[0])
            self.assertEqual(events[0]["retry_attempt"], 2)
            self.assertIsNone(options[0]["web_client"].token)
            self.assertTrue(options[0]["web_client"].logger.disabled)
            source._observe(False)
            source._observe(False)
            self.assertEqual(source.disconnects, 1)
            source._observe(True)
            source._observe(False)
            self.assertEqual(source.disconnects, 2)
            request.type = "interactive"
            source._receive(client, request)
            self.assertEqual(calls[-1], "ack")
            request.type = "events_api"
            source.on_event = lambda event: (_ for _ in ()).throw(ValueError("synthetic body"))
            source._receive(client, request)
            self.assertEqual(source.failures, 1)
            source.stop()
            self.assertEqual(calls[-1], "close")


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.store = LedgerStore(":memory:")
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.store.set_cursor("slack", "C0000000001", "100.0")
        self.events, self.calls, self.sleeps = [], [], []

        def receive(event):
            self.events.append(event)
            self.store.record_event(event)

        self.source = SlackReconcileSource(SourceConfig("slack_reconcile", True, 3600),
                                            receive, "dummy.env", "USER", self.store,
                                            sleep=self.sleeps.append)

    def fake_client(self, responses):
        calls = self.calls

        class FakeWeb:
            def api_call(self, method, **kwargs):
                calls.append((method, kwargs))
                result = responses.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
        self.source.client = FakeWeb()

    def test_pagination_order_cursor_and_no_write(self):
        self.fake_client([
            {"messages": [{"ts": "300.0", "text": "synthetic body"}],
             "response_metadata": {"next_cursor": "dummy-next"}},
            {"messages": [{"ts": "200.0"}, {"ts": "100.0"}]},
        ])
        self.source.catch_up()
        self.assertEqual([event["ts"] for event in self.events], ["200.0", "300.0"])
        self.assertEqual(self.store.get_cursor("slack", "C0000000001"), "300.0")
        self.assertEqual(self.calls[1][1]["params"]["cursor"], "dummy-next")
        self.assertTrue(all("text" not in event for event in self.events))
        with self.assertRaisesRegex(ValueError, "read-only"):
            self.source._call("chat.postMessage", text="synthetic body")
        self.assertEqual(len(self.calls), 2)

    def test_rate_limit_and_callback_failure_keeps_cursor(self):
        error = RuntimeError("synthetic failure")
        error.response = SimpleNamespace(status_code=429, headers={"Retry-After": "2"})
        self.fake_client([error, {"messages": [{"ts": "200.0"}]}])
        self.source.catch_up()
        self.assertEqual(self.sleeps, [2.0])
        self.fake_client([{"messages": [{"ts": "300.0"}, {"ts": "400.0"}]}])
        self.source.on_event = lambda event: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
        self.source.catch_up()
        self.assertEqual(self.source.failures, 1)
        self.assertEqual(self.store.get_cursor("slack", "C0000000001"), "200.0")

    def test_failed_page_keeps_cursor(self):
        self.fake_client([{"messages": [{"ts": "300.0"}],
                           "response_metadata": {"next_cursor": "next"}}, RuntimeError("synthetic")])
        self.source.catch_up()
        self.assertEqual(self.source.failures, 1)
        self.assertEqual(self.events, [])
        self.assertEqual(self.store.get_cursor("slack", "C0000000001"), "100.0")

    def test_callback_failure_does_not_skip_other_channel(self):
        self.store.set_cursor("slack", "C0000000002", "100.0")
        self.fake_client([{"messages": [{"ts": "200.0"}]},
                          {"messages": [{"ts": "300.0"}]}])
        receive = self.source.on_event

        def fail_first_channel(event):
            if event["channel"] == "C0000000001":
                raise RuntimeError("synthetic failure")
            receive(event)

        self.source.on_event = fail_first_channel
        with patch.object(self.store, "list_channels",
                          return_value=["C0000000001", "C0000000002"]):
            self.source.catch_up()
        self.assertEqual(self.source.failures, 1)
        self.assertEqual(self.store.get_cursor("slack", "C0000000001"), "100.0")
        self.assertEqual(self.store.get_cursor("slack", "C0000000002"), "300.0")
        self.assertEqual([event["channel"] for event in self.events], ["C0000000002"])

    def test_startup_failure_still_runs_periodic_pass(self):
        import threading
        calls = []
        done = threading.Event()
        def catch_up():
            calls.append(threading.current_thread())
            if len(calls) == 1:
                raise RuntimeError("synthetic failure")
            done.set()
        with patch.dict(sys.modules, sdk_modules(web_class=lambda **kwargs: object())), patch(
                "slack_dispatch.ingest.slack_reconcile.load_token", return_value="dummy"), patch.object(
                self.source, "catch_up", side_effect=catch_up):
            self.source.start()
            try:
                self.assertEqual(self.source.failures, 1)
                self.assertEqual(len(calls), 1)
                self.source.request_catchup()
                self.assertTrue(done.wait(2))
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(thread is self.source._thread for thread in calls))
            finally:
                self.source.stop()

    def test_first_run_lookback_and_startup_pass(self):
        self.store.record_event(normalize_slack_event(FIXTURES[7], STAMP))
        self.store._db.execute("DELETE FROM cursors")
        calls = self.calls
        self.source.clock = lambda: 1000000

        class FakeWeb:
            def __init__(self, **kwargs):
                pass

            def api_call(self, method, **kwargs):
                calls.append((method, kwargs))
                return {"messages": []}

        with patch.dict(sys.modules, sdk_modules(web_class=FakeWeb)), patch(
                "slack_dispatch.ingest.slack_reconcile.load_token", return_value="xoxp-FAKE-example"), patch(
                "slack_dispatch.ingest.slack_reconcile.time.time", return_value=1000000):
            self.source.start()
            try:
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][1]["params"]["oldest"], "395200.000000")
            finally:
                self.source.stop()
