"""Read-only history catch-up supplements Socket Mode intake."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import math
import logging
import threading
import time

from .normalize import normalize_slack_event, safe, IDENTIFIER, TIMESTAMP
from .tokens import load_token, quiet_logger

LOG = logging.getLogger(__name__)
HYDRATION_RETRY_LIMIT = 3  # failed retries per root after the first failed fetch
# Slack error codes that are not expected to clear soon. A reply parent failing with
# one of these is skipped for config.permanent_error_ttl_seconds, then retried once.
PERMANENT_REPLY_ERRORS = frozenset({"thread_not_found", "channel_not_found", "not_in_channel",
                                    "message_not_found", "is_archived"})

READ_METHODS = frozenset({"conversations.history", "conversations.replies",
                          "conversations.info", "conversations.list", "auth.test"})


class SlackReconcileSource:
    # Standalone callers keep the original contract: start returns after the first pass.
    wait_for_first_pass = True

    def __init__(self, config, on_event, env_file, user_token_key, store, *, sleep=None, clock=None, excluded_channels=None):
        self.config, self.on_event, self.store = config, on_event, store
        self.env_file, self.user_token_key = env_file, user_token_key
        self.client = None
        self.failures = 0
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._thread = None
        self._pass_lock = threading.Lock()
        self.clock = clock or time.time
        self.excluded_channels = set(config.excluded_channels if excluded_channels is None else excluded_channels)
        self._discovered = set()
        self._last_discovery = None
        self.owner_user = None
        self.last_catchup_at = None
        self.on_catchup = None
        self.on_context = None
        self._wake = threading.Event()
        self._hydration_lock = threading.Lock()
        self._hydration_jobs = {}
        self._hydrated = {}
        self._hydration_failures = {}
        self._abandoned_roots = set()
        self.pending_roots = set()
        # (channel, parent ts) -> pass time of the last permanent replies error.
        self.skipped_parents = {}
        self._permanent_warned = set()  # parents already reported once at WARNING
        self.summary = {}

    def _call(self, method, **params):
        if method not in READ_METHODS:
            raise ValueError("Slack method is not read-only")
        while not self._stop.is_set():
            try:
                return self.client.api_call(method, http_verb="GET", params=params)
            except Exception as error:
                response = getattr(error, "response", None)
                if response is None or getattr(response, "status_code", None) != 429:
                    failure = RuntimeError("Slack read request failed")
                    failure.error_class = type(error).__name__
                    data = getattr(response, "data", response)
                    failure.slack_error = safe(data.get("error"), r"[a-z_]{1,64}") if isinstance(data, dict) else None
                    raise failure from None
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
            before = self.failures
            self.summary = dict(channels=0, roots=0, parents=0, replies=0, hydrated=0,
                                duplicates=0, prehandled=0, failures=0, thread_candidates=0,
                                thread_skipped_age=0, backfill=0, permanent_skipped=0)
            self._pass_started = self.clock()
            if self.config.discover_channels and (self._last_discovery is None
                    or self.clock() - self._last_discovery >= 6 * 3600):
                try:
                    self._discover_channels()
                except Exception as error:
                    self.failures += 1
                    self._log_failure("discovery", error)
            with self._hydration_lock:
                pending = sorted(self.pending_roots)
            for channel, thread in pending:
                try:
                    event = self.hydrate_root(channel, thread)
                    if event is not None and self.on_context:
                        self.on_context(event)
                except Exception as error:
                    self.failures += 1
                    LOG.info("Hydration failed class=%s", type(error).__name__)
            channels = set(self.store.list_channels("slack")) | self._discovered
            for channel in sorted(channels - self.excluded_channels):
                if self._stop.is_set():
                    break
                if safe(channel, IDENTIFIER) is None:
                    continue
                self.summary["channels"] += 1
                try:
                    self._catch_up_channel(channel)
                except Exception as error:
                    self.failures += 1
                    self._log_failure(channel, error)
            self.summary["failures"] = self.failures - before
            if not self.summary["failures"] and not self._stop.is_set():
                self.last_catchup_at = datetime.fromtimestamp(self._pass_started, timezone.utc).astimezone().isoformat()
                if self.on_catchup:
                    self.on_catchup(self.last_catchup_at)
            LOG.info("Reconcile pass channels=%d roots=%d parents=%d replies=%d hydrated=%d duplicates=%d "
                     "prehandled=%d failures=%d thread_candidates=%d thread_skipped_age=%d "
                     "permanent_skipped=%d; backfill delivered %d (no wake)",
                     *(self.summary[key] for key in ("channels", "roots", "parents", "replies", "hydrated",
                                                     "duplicates", "prehandled", "failures", "thread_candidates",
                                                     "thread_skipped_age", "permanent_skipped", "backfill")))

    @staticmethod
    def _log_failure(channel, error):
        LOG.info("Reconcile channel=%s class=%s error=%s", channel,
                 getattr(error, "error_class", type(error).__name__),
                 getattr(error, "slack_error", None))

    def request_catchup(self):
        self._wake.set()

    def _discover_channels(self):
        channels, page, visited = set(), None, set()
        while True:
            params = dict(types="public_channel,private_channel,im,mpim", limit=200)
            if page:
                params["cursor"] = page
            response = self._call("conversations.list", **params)
            if not response.get("ok", True):
                raise RuntimeError("Slack channel discovery failed")
            for channel in response.get("channels", []):
                identifier = safe(channel.get("id"), IDENTIFIER) if isinstance(channel, dict) else None
                if identifier and identifier not in self.excluded_channels:
                    channels.add(identifier)
            page = response.get("response_metadata", {}).get("next_cursor")
            if not page:
                break
            if not isinstance(page, str) or page in visited:
                raise RuntimeError("Invalid discovery pagination cursor")
            visited.add(page)
        self._discovered.update(channels)
        self._last_discovery = self.clock()

    def _pages(self, method, **params):
        messages, page, visited = [], None, set()
        while True:
            response = self._call(method, **dict(params, **({"cursor": page} if page else {})))
            if not response.get("ok", True):
                error = RuntimeError("Slack read response failed")
                error.slack_error = safe(response.get("error"), r"[a-z_]{1,64}")
                raise error
            for message in response.get("messages", []):
                if not isinstance(message, dict) or safe(message.get("ts"), TIMESTAMP) is None:
                    raise RuntimeError("Invalid message timestamp")
                messages.append(message)
            page = response.get("response_metadata", {}).get("next_cursor")
            if not page:
                if response.get("has_more"):
                    raise RuntimeError("Pagination cursor missing")
                return sorted(messages, key=lambda row: Decimal(row["ts"]))
            if not isinstance(page, str) or page in visited:
                raise RuntimeError("Invalid pagination cursor")
            visited.add(page)

    def _event(self, channel, message, origin="reconcile"):
        identity = hashlib.sha256(f"slack:{channel}:{message['ts']}".encode()).hexdigest()
        event = normalize_slack_event(
            {"event_id": "Ev" + identity, "event": dict(message, type="message", channel=channel)},
            datetime.now().astimezone().isoformat(), store_body=self.config.store_body)
        event["origin"] = origin
        return event

    def _deliver(self, channel, message, replies=()):
        event = self._event(channel, message)
        later = [row["ts"] for row in replies if self.owner_user and row.get("user") == self.owner_user
                 and Decimal(row["ts"]) > Decimal(message["ts"])]
        if message.get("user") != self.owner_user and later:
            event["prehandled_by"] = "owner_reply:" + later[-1]
        result = self.on_event(event)
        if getattr(result, "backfill", False) is True:
            self.summary["backfill"] += 1
        if getattr(result, "duplicate", False) is True:
            self.summary["duplicates"] += 1
        elif isinstance(getattr(result, "message_id", None), str):
            row = self.store.get_message(result.message_id)
            if row and (row.get("note") or "").startswith("prehandled:"):
                self.summary["prehandled"] += 1

    def _catch_up_channel(self, channel):
        started = self._pass_started
        cursor = self.store.get_cursor("slack", channel)
        oldest = cursor or f"{max(0, started - self.config.lookback_days * 86400):.6f}"
        days = self.config.replies_lookback_days or self.config.lookback_days
        window = Decimal(f"{max(0, started - days * 86400):.6f}")
        history_start = min(Decimal(oldest), window) if self.config.replies else Decimal(oldest)
        history = self._pages("conversations.history", channel=channel, oldest=f"{history_start:.6f}",
                              latest=f"{started:.6f}", inclusive=True, limit=200)
        roots = [row for row in history if Decimal(row["ts"]) > Decimal(oldest)]
        self.summary["roots"] += len(roots)
        fetched, replies_complete = {}, True
        if self.config.replies:
            fetched, replies_complete = self._fetch_replies(channel, history, window)
        # Root delivery and the root cursor never depend on the replies step. A root
        # whose replies could not be fetched routes without reply-based evidence.
        newest = cursor
        for message in roots:
            self._deliver(channel, message, fetched.get(message["ts"], ()))
            if newest is None or Decimal(message["ts"]) > Decimal(newest):
                newest = message["ts"]
        if newest is not None and newest != cursor:
            self.store.set_cursor("slack", channel, newest)
        replies = [(row, rows) for rows in fetched.values() for row in rows]
        for message, siblings in sorted(replies, key=lambda item: Decimal(item[0]["ts"])):
            self._deliver(channel, message, siblings)
        if self.config.replies and replies_complete:
            self.store.set_cursor("slack_replies", channel, f"{started:.6f}")

    def _fetch_replies(self, channel, history, window):
        """Fetch replies per parent; a failed parent is skipped and keeps the replies cursor.

        A parent with a permanent Slack error is skipped for permanent_error_ttl_seconds,
        then retried once; it does not hold the replies cursor and counts as
        permanent_skipped, not a failure. Only the first permanent error logs a WARNING.
        """
        started = self._pass_started
        fetched, complete = {}, True
        try:
            reply_cursor = self.store.get_cursor("slack_replies", channel)
            reply_oldest = (max(Decimal(0), Decimal(reply_cursor) - self.config.replies_overlap_seconds)
                            if reply_cursor else window)
            parents = self._reply_parents(channel, history, window, reply_cursor, reply_oldest)
        except Exception as error:
            self._reply_failure(channel, None, error)
            return fetched, False
        for parent in parents:
            if self._stop.is_set():
                return fetched, False
            key = (channel, parent)
            skipped_at = self.skipped_parents.get(key)
            if skipped_at is not None and started - skipped_at < self.config.permanent_error_ttl_seconds:
                self.summary["permanent_skipped"] += 1
                continue
            self.summary["parents"] += 1
            try:
                rows = self._pages("conversations.replies", channel=channel, ts=parent,
                                   oldest=f"{reply_oldest:.6f}", latest=f"{started:.6f}", inclusive=False, limit=200)
            except Exception as error:
                code = getattr(error, "slack_error", None)
                if code in PERMANENT_REPLY_ERRORS:
                    self.skipped_parents[key] = started
                    self.summary["permanent_skipped"] += 1
                    if key in self._permanent_warned:
                        LOG.debug("Reconcile replies still skipped channel=%s parent=%s error=%s",
                                  channel, parent, code)
                    else:
                        self._permanent_warned.add(key)
                        LOG.warning("Reconcile replies skipped permanently channel=%s parent=%s error=%s",
                                    channel, parent, code)
                    continue
                # A transient error after the TTL is an ordinary failure, retried next pass.
                self.skipped_parents.pop(key, None)
                complete = False
                self._reply_failure(channel, parent, error)
                continue
            self.skipped_parents.pop(key, None)
            self._permanent_warned.discard(key)
            fetched[parent] = [dict(row, thread_ts=parent) for row in rows
                               if Decimal(row["ts"]) > reply_oldest and row["ts"] != parent]
            self.summary["replies"] += len(fetched[parent])
        return fetched, complete

    def _reply_parents(self, channel, history, window, reply_cursor, reply_oldest):
        started = Decimal(f"{self._pass_started:.6f}")
        parents = set()
        for row in history:
            if not window <= Decimal(row["ts"]) <= started:
                continue
            if ((not reply_cursor and row.get("reply_count", 0) > 0) or
                    (reply_cursor and safe(row.get("latest_reply"), TIMESTAMP)
                     and Decimal(row["latest_reply"]) > reply_oldest)):
                parents.add(row["ts"])
        # Thread activity = max(thread_ts, newest ledger message ts in that thread).
        activity = {}
        for row in self.store._rows("SELECT thread_ts, ts FROM messages WHERE source='slack' "
                                    "AND channel=? AND thread_ts IS NOT NULL", (channel,)):
            thread, ts = safe(row["thread_ts"], TIMESTAMP), safe(row["ts"], TIMESTAMP)
            if thread and ts:
                activity[thread] = max(activity.get(thread, Decimal(thread)), Decimal(ts))
        candidates = {thread for thread, latest in activity.items() if latest > window}
        if self.config.bound_threads:
            for row in self.store._rows("SELECT thread_ts FROM bindings WHERE source='slack' AND channel=?",
                                        (channel,)):
                thread = safe(row["thread_ts"], TIMESTAMP)
                if thread:
                    candidates.add(thread)
                    activity.setdefault(thread, Decimal(thread))
        floor = started - self.config.bound_threads_max_age_days * 86400
        self.summary["thread_candidates"] += len(candidates)
        for thread in candidates - parents:
            if activity[thread] >= floor:
                parents.add(thread)
            else:
                self.summary["thread_skipped_age"] += 1
        return sorted(parents, key=Decimal)

    def _reply_failure(self, channel, parent, error):
        self.failures += 1
        LOG.warning("Reconcile replies failed channel=%s parent=%s class=%s error=%s", channel, parent,
                    getattr(error, "error_class", type(error).__name__),
                    getattr(error, "slack_error", None))

    def hydrate_root(self, channel, thread_ts):
        """Wait at most ten seconds; keep an outstanding request for later collection.

        Excluded channels and abandoned roots return None without a request.
        """
        if channel in self.excluded_channels:
            return None
        key = channel, thread_ts
        with self._hydration_lock:
            if key in self._hydrated:
                return self._hydrated[key]
            if key in self._abandoned_roots:
                return None
            job = self._hydration_jobs.get(key)
            if job is None:
                job = {"done": threading.Event()}
                self._hydration_jobs[key] = job
                def fetch():
                    try:
                        response = self._call("conversations.replies", channel=channel,
                                              ts=thread_ts, limit=1, inclusive=True)
                        rows = response.get("messages", [])
                        if not response.get("ok", True) or not rows or rows[0].get("ts") != thread_ts:
                            raise RuntimeError("Root unavailable")
                        job["event"] = self._event(channel, rows[0], "hydrate")
                    except Exception as error:
                        job["error"] = type(error).__name__
                    finally:
                        job["done"].set()
                threading.Thread(target=fetch, daemon=True).start()
        if not job["done"].wait(10):
            with self._hydration_lock:
                self._hydration_failed(key)
            raise TimeoutError("Root hydration timed out")
        with self._hydration_lock:
            self._hydration_jobs.pop(key, None)
            if "error" in job:
                self._hydration_failed(key)
                raise RuntimeError("Root hydration failed")
            self._hydrated[key] = job["event"]
            self._hydration_failures.pop(key, None)
            self.pending_roots.discard(key)
            self.summary["hydrated"] = self.summary.get("hydrated", 0) + 1
            return job["event"]

    def _hydration_failed(self, key):
        """Count one failed attempt; give up after the retry limit. Caller holds the lock."""
        count = self._hydration_failures.get(key, 0) + 1
        if count > HYDRATION_RETRY_LIMIT:
            self._hydration_failures.pop(key, None)
            self._hydration_jobs.pop(key, None)
            self.pending_roots.discard(key)
            self._abandoned_roots.add(key)
            LOG.info("Hydration abandoned root=slack:%s:%s attempts=%d", key[0], key[1], count)
        else:
            self._hydration_failures[key] = count
            self.pending_roots.add(key)

    def open_client(self):
        """Load the token and build the read-only client without starting intake."""
        if self.client is None:
            from slack_sdk import WebClient
            token = load_token(self.env_file, self.user_token_key, "xoxp-")
            self.client = WebClient(token=token, logger=quiet_logger(), retry_handlers=[], timeout=10)
        return self.client

    def fetch_message(self, channel, ts, thread_ts=None):
        """Return the raw message with exactly this ts, or None when Slack has none.

        Replies use conversations.replies on their parent, roots use conversations.history,
        both bounded with latest=oldest=ts and inclusive=true. Callers must not log the result.
        """
        if channel in self.excluded_channels:
            return None
        bounds = dict(channel=channel, latest=ts, oldest=ts, inclusive=True, limit=10)
        if thread_ts and thread_ts != ts:
            response = self._call("conversations.replies", ts=thread_ts, **bounds)
        else:
            response = self._call("conversations.history", **bounds)
        if not response.get("ok", True):
            error = RuntimeError("Slack read response failed")
            error.slack_error = safe(response.get("error"), r"[a-z_]{1,64}")
            raise error
        for message in response.get("messages", []) or []:
            if isinstance(message, dict) and message.get("ts") == ts:
                return message
        return None

    def start(self):
        if self._thread is not None:
            return
        from slack_sdk import WebClient
        token = load_token(self.env_file, self.user_token_key, "xoxp-")
        self._stop.clear()
        self.client = WebClient(token=token, logger=quiet_logger(), retry_handlers=[], timeout=10)
        ready = threading.Event()
        def loop():
            try:
                self.catch_up()
            except Exception as error:
                self.failures += 1
                self._log_failure("startup", error)
            finally:
                ready.set()
            while not self._stop.is_set():
                self._wake.wait(self.config.interval_seconds)
                self._wake.clear()
                if self._stop.is_set():
                    break
                try:
                    self.catch_up()
                except Exception as error:
                    self.failures += 1
                    self._log_failure("pass", error)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        if self.wait_for_first_pass:
            ready.wait()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join()
        self._thread = None
        self.client = None
