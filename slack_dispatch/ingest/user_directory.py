"""Bot flag rule and user directory (P16): a post by a human is a human post.

Teammates who post through their own assistant apps (a user token used by an app) produce
messages with a human `user` id plus `bot_id`, `app_id` and `bot_profile`. Real bot users
produce messages with the same fields and a bot `user` id. Only the user profile
(`users.info` field `is_bot`) tells them apart, so `bot_id`, `app_id` and `bot_profile`
by themselves no longer mark a message as a bot message.

`is_bot_message` is the one rule. `UserDirectory` caches `users.info` answers in the
ledger table `user_directory`, modelled on the P15 `MembershipCache`. Construction
performs no network or credential reads. A failed lookup keeps an expired cached row's
value; a user with no cached row is treated as human for that event (fail open): a bot
post reaching a lane costs one `done`, a lost human post costs a missed reply. After a
failure the user is not looked up again for `failure_cooldown_seconds` (in memory only).

P20: the directory also supplies display names. With `user_directory.resolve_names` the
pipeline asks `ensure_name` for every author it has not seen (off the hot path, from its
periodic flush), so the table gradually holds every active user's `name` (`real_name`,
else `name`). `refresh` is the operator backfill (`directory refresh`), and
`open_reconcile_client(config)` builds the same read-only client the reconcile source uses.
"""
from datetime import datetime, timezone
import logging
import threading
import time

from .normalize import IDENTIFIER, safe
from .slack_reconcile import READ_METHODS, SlackReconcileSource

LOG = logging.getLogger(__name__)
METHOD = "users.info"
WARN_SECONDS = 3600
DEFAULT_TTL_SECONDS = 86400
DEFAULT_FAILURE_COOLDOWN_SECONDS = 300

if METHOD not in READ_METHODS:  # pragma: no cover - guarded by the reconcile allowlist
    raise RuntimeError("users.info must be on the read-only allowlist")


def message_author(message):
    """The author of an event (`user`) or of a ledger row (`author`)."""
    return message.get("user", message.get("author"))


def known_bot_users(config):
    bots = getattr(config, "bot_messages", None)
    return tuple(getattr(bots, "known_bot_users", ()) or ())


def is_bot_message(event, config, directory=None) -> bool:
    """True iff (a) no author, (b) subtype `bot_message`, (c) the author is in
    `bot_messages.known_bot_users`, or (d) `directory` is given and says the author is a
    bot. `bot_id`, `app_id` and `bot_profile` alone do not mark a bot. `config` may be
    None (no known bot users)."""
    author = message_author(event)
    if not author:
        return True
    if event.get("subtype") == "bot_message":
        return True
    if author in known_bot_users(config):
        return True
    return bool(directory is not None and directory.is_bot(author))


def carries_app_evidence(event):
    """The event was posted through an app (`bot_id` or `app_id`); only then is the
    directory consulted, so humans posting by hand never cost a lookup."""
    return bool(event.get("bot_id") or event.get("app_id"))


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat()


def _epoch(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def parse_user(response):
    """A directory row from a `users.info` response, or None when the answer is unusable."""
    user = response.get("user") if isinstance(response, dict) else None
    if not isinstance(user, dict):
        return None
    user_id = safe(user.get("id"), IDENTIFIER)
    if user_id is None:
        return None
    # P20: the display name is `real_name` when set, else the handle `name`.
    name = next((value for value in (user.get("real_name"), user.get("name"))
                 if isinstance(value, str) and value.strip()), None)
    return dict(user=user_id, is_bot=int(user.get("is_bot") is True),
                is_app_user=int(user.get("is_app_user") is True),
                deleted=int(user.get("deleted") is True),
                name=name.strip()[:200] if name else None)


class UserDirectory:
    """`users.info` answers cached in the ledger table `user_directory`.

    `client` is a Slack client (anything with `api_call(method, http_verb, params)`) or a
    zero-argument callable returning one, so the service can share the reconcile source's
    read-only client, which exists only after that source starts. Without a client every
    lookup fails open (human) and nothing is cached.
    """

    def __init__(self, client, store, ttl_seconds=DEFAULT_TTL_SECONDS, known_bot_users=(), *,
                 clock=None, failure_cooldown_seconds=DEFAULT_FAILURE_COOLDOWN_SECONDS):
        self._client = client
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.failure_cooldown_seconds = failure_cooldown_seconds
        self._failed_at = {}  # user -> time of the last failed lookup (memory only)
        self.known_bot_users = frozenset(known_bot_users)
        self.clock = clock or time.time
        self.lookups = 0
        self.failures = 0
        # P20: names stored by `ensure_name` (heartbeat counter `directory_names`).
        self.names = 0
        self._warned = {}
        self._lock = threading.Lock()

    def client(self):
        client = self._client
        if callable(client) and not hasattr(client, "api_call"):
            client = client()
        return client

    def is_bot(self, user):
        """True for a bot user, False for a human.

        A failed lookup (or a user still in its failure cooldown) keeps the cached row's
        value even when it is older than the TTL; without a cached row the user counts as
        human (fail open). Failures are never written to the table.
        """
        if not user:
            return True
        if user in self.known_bot_users:
            return True
        with self._lock:
            row = self.store.get_user_directory(user)
            if row is not None and not self._stale(row):
                return bool(row["is_bot"])
            fetched = None if self._cooling_down(user) else self._lookup(user)
            if fetched is None:
                return bool(row["is_bot"]) if row is not None else False
            return bool(fetched["is_bot"])

    def ensure_name(self, user):
        """P20: make sure the user has a fresh directory row (and so a display name).

        Looks the user up only when there is no row or the row is older than the TTL, with
        the same cooldown and fail-open rules as `is_bot`. Returns the row, the old row when
        the lookup failed or is cooling down, or None. fix1: the users.info call runs without
        the directory lock (only the checks and the counters take it), so `is_bot` on the
        event path never waits for a name lookup; the row is stored through the ledger's
        own transaction.
        """
        if not user:
            return None
        with self._lock:
            row = self.store.get_user_directory(user)
            if row is not None and not self._stale(row):
                return row
            if self._cooling_down(user):
                return row
            self.lookups += 1
        try:
            fetched = self._request(user)
            self.store.put_user_directory(fetched, _stamp(self.clock()))
        except _LookupFailed as error:
            with self._lock:
                self._fail(user, error.error_class, error.slack_error)
            return row
        except Exception as error:
            with self._lock:
                self._fail(user, type(error).__name__, None)
            return row
        with self._lock:
            self._failed_at.pop(user, None)
            if fetched.get("name"):
                self.names += 1
        return fetched

    def refresh(self, user):
        """Operator backfill (P20 `directory refresh`): look the user up now, whatever the
        TTL. Returns the stored row, or None when the lookup failed."""
        if not user:
            return None
        with self._lock:
            fetched = self._lookup(user)
            if fetched is not None and fetched.get("name"):
                self.names += 1
            return fetched

    def _cooling_down(self, user):
        failed = self._failed_at.get(user)
        if failed is None:
            return False
        now = self.clock()
        if now < failed or now - failed >= self.failure_cooldown_seconds:
            del self._failed_at[user]
            return False
        return True

    def _stale(self, row):
        seen = _epoch(row.get("seen_at"))
        now = self.clock()
        return seen is None or now < seen or now - seen >= self.ttl_seconds

    def _request(self, user):
        """One users.info call parsed into a directory row; raises _LookupFailed. No state
        changes, so callers may run it without the directory lock (fix1)."""
        client = self.client()
        if client is None:
            raise _LookupFailed("NoClient", None)
        try:
            response = client.api_call(METHOD, http_verb="GET", params={"user": user})
        except Exception as error:
            data = getattr(getattr(error, "response", None), "data", None)
            raise _LookupFailed(type(error).__name__,
                                data.get("error") if isinstance(data, dict) else None) from None
        if not isinstance(response, dict) and hasattr(response, "data"):
            response = response.data
        if not isinstance(response, dict) or not response.get("ok", True):
            raise _LookupFailed("SlackResponse",
                                response.get("error") if isinstance(response, dict) else None)
        row = parse_user(response)
        if row is None or row["user"] != user:
            raise _LookupFailed("MissingUser", None)
        return row

    def _lookup(self, user):
        self.lookups += 1
        try:
            row = self._request(user)
            self.store.put_user_directory(row, _stamp(self.clock()))
            self._failed_at.pop(user, None)
            return row
        except _LookupFailed as error:
            self._fail(user, error.error_class, error.slack_error)
        except Exception as error:
            self._fail(user, type(error).__name__, None)
        return None

    def _fail(self, user, error_class, slack_error):
        # Nothing is written to the table; the user cools down in memory before the next
        # lookup. One WARNING per user per hour.
        self.failures += 1
        now = self.clock()
        self._failed_at[user] = now
        last = self._warned.get(user)
        if last is None or now < last or now - last >= WARN_SECONDS:
            self._warned[user] = now
            LOG.warning("User directory lookup failed user=%s class=%s error=%s; treated as human",
                        user, error_class, safe(slack_error, r"[a-z_]{1,64}"))

    def snapshot(self):
        return {"directory_lookups": self.lookups, "directory_failures": self.failures}


def open_reconcile_client(config):
    """The read-only Slack client of the first slack_reconcile source (P20 backfill).

    Built like the service's `--rehydrate-bodies` mode: the source's user token, the
    reconcile read allowlist, no intake started. Raises LookupError without a source.
    """
    entries = [entry for entry in config.sources if entry.kind == "slack_reconcile"]
    entry = next((row for row in entries if row.enabled), entries[0] if entries else None)
    if entry is None:
        raise LookupError("No slack_reconcile source configured")
    source = SlackReconcileSource(entry, lambda event: None, entry.env_file, entry.user_token_key,
                                  None, excluded_channels=config.excluded_channels)
    return source.open_client()


class _LookupFailed(Exception):
    def __init__(self, error_class, slack_error):
        super().__init__("User directory lookup failed")
        self.error_class = error_class
        self.slack_error = slack_error
