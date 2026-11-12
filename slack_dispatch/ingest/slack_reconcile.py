"""Read-only history catch-up supplements Socket Mode intake."""
from datetime import datetime
from decimal import Decimal
import hashlib
import math
import threading
import time

from .normalize import normalize_slack_event, safe, IDENTIFIER, TIMESTAMP
from .tokens import load_token, quiet_logger

READ_METHODS = frozenset({"conversations.history", "conversations.replies",
                          "conversations.info", "auth.test"})


class SlackReconcileSource:
    def __init__(self, config, on_event, env_file, user_token_key, store, *, sleep=None):
        self.config, self.on_event, self.store = config, on_event, store
        self.env_file, self.user_token_key = env_file, user_token_key
        self.client = None
        self.failures = 0
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._thread = None
        self._pass_lock = threading.Lock()

    def _call(self, method, **params):
        if method not in READ_METHODS:
            raise ValueError("Slack method is not read-only")
        while not self._stop.is_set():
            try:
                return self.client.api_call(method, http_verb="GET", params=params)
            except Exception as error:
                response = getattr(error, "response", None)
                if response is None or getattr(response, "status_code", None) != 429:
                    raise RuntimeError("Slack read request failed") from None
                headers = {k.lower(): v for k, v in response.headers.items()}
                try:
                    delay = float(headers.get("retry-after", "1"))
                    if not math.isfinite(delay) or delay < 0:
                        delay = 1
                except (TypeError, ValueError):
                    delay = 1
                self._sleep(max(delay, 0.01))
        raise RuntimeError("Reconcile stopped")

    def catch_up(self):
        with self._pass_lock:
            for channel in self.store.list_channels("slack"):
                if self._stop.is_set():
                    return
                if safe(channel, IDENTIFIER) is None:
                    continue
                try:
                    self._catch_up_channel(channel)
                except Exception:
                    # Keep failed channel cursors unchanged and continue the pass.
                    self.failures += 1

    def _catch_up_channel(self, channel):
        cursor = self.store.get_cursor("slack", channel)
        oldest = cursor or f"{max(0, time.time() - self.config.lookback_days * 86400):.6f}"
        messages, page, visited = [], None, set()
        while True:
            params = dict(channel=channel, oldest=oldest, inclusive=False, limit=200)
            if page:
                params["cursor"] = page
            response = self._call("conversations.history", **params)
            if not response.get("ok", True):
                raise RuntimeError("Slack history request failed")
            for message in response.get("messages", []):
                if not isinstance(message, dict):
                    raise RuntimeError("Invalid history message")
                ts = safe(message.get("ts"), TIMESTAMP)
                if ts is None:
                    raise RuntimeError("Invalid history timestamp")
                if Decimal(ts) > Decimal(oldest):
                    messages.append(message)
            page = response.get("response_metadata", {}).get("next_cursor")
            if not page:
                if response.get("has_more"):
                    raise RuntimeError("History pagination cursor missing")
                break
            if not isinstance(page, str) or page in visited:
                raise RuntimeError("Invalid history pagination cursor")
            visited.add(page)
        newest = cursor
        for message in sorted(messages, key=lambda row: Decimal(row["ts"])):
            if self._stop.is_set():
                return
            ts = message["ts"]
            identity = hashlib.sha256(f"slack:{channel}:{ts}".encode()).hexdigest()
            event = normalize_slack_event(
                {"event_id": "Ev" + identity,
                 "event": dict(message, type="message", channel=channel)},
                datetime.now().astimezone().isoformat(), store_body=self.config.store_body)
            self.on_event(event)
            if newest is None or Decimal(ts) > Decimal(newest):
                newest = ts
        # Callbacks must durably record events before returning.
        # A failed page/callback leaves the previous cursor unchanged.
        if newest is not None and newest != cursor:
            self.store.set_cursor("slack", channel, newest)

    def start(self):
        if self._thread is not None:
            return
        from slack_sdk import WebClient
        token = load_token(self.env_file, self.user_token_key, "xoxp-")
        self._stop.clear()
        self.client = WebClient(token=token, logger=quiet_logger(), retry_handlers=[])
        try:
            self.catch_up()
        except Exception:
            self.failures += 1

        def loop():
            while not self._stop.wait(self.config.interval_seconds):
                try:
                    self.catch_up()
                except Exception:
                    self.failures += 1
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join()
        self._thread = None
        self.client = None
