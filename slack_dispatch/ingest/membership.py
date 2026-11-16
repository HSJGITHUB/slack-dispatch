"""Channel membership cache (P15): which conversations the operator belongs to.

`MembershipCache` reads `users.conversations` (public and private channels, group DMs
and DMs, paginated) through the reconcile source's read-only client and keeps the
result in the ledger table `channel_membership`. The service and the reconcile source
ask it whether a channel is a member channel; the pipeline excludes events from other
channels under `membership.policy: exclude`.

Construction performs no network or credential reads. A refresh failure keeps the
previous table and logs one WARNING line with the exception class and Slack error code.
A successful response with no channels is treated as a failure too: the previous table
and in-memory set are kept, so an empty answer never blocks every non-DM channel.
"""
from datetime import datetime, timezone
import logging
import threading
import time

from .normalize import IDENTIFIER, safe

LOG = logging.getLogger(__name__)
METHOD = "users.conversations"
TYPES = "public_channel,private_channel,mpim,im"
PAGE_LIMIT = 200
NOT_MEMBER = "excluded:not_member"
POLICIES = ("exclude", "off")


def is_direct(channel, channel_type=None):
    """A DM or group DM: channel type `im` or `mpim`, or a `D` conversation id."""
    return channel_type in ("im", "mpim") or str(channel or "").startswith("D")


def member_status(channel, members, loaded, channel_type=None):
    """True for a member channel, False for a non-member, None while nothing is loaded.

    A DM or group DM that is not in the cache yet counts as a member: user-scoped events
    for direct conversations are delivered only to their participants, and a new DM must
    not wait for the next refresh (see CONTRACT_NOTES).
    """
    if not loaded or not channel:
        return None
    if channel in members:
        return True
    return True if is_direct(channel, channel_type) else False


def _kind(row):
    if row.get("is_im"):
        return "im"
    if row.get("is_mpim"):
        return "mpim"
    if row.get("is_private") or row.get("is_group"):
        return "private_channel"
    return "public_channel"


def _text(value, limit=200):
    return value[:limit] if isinstance(value, str) and value else None


def parse_conversations(rows):
    """Membership rows from users.conversations channel objects; invalid ids are skipped."""
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        channel = safe(row.get("id"), IDENTIFIER)
        if channel is None:
            continue
        result[channel] = dict(channel=channel, kind=_kind(row), name=_text(row.get("name")),
                               partner_user=safe(row.get("user"), IDENTIFIER),
                               is_member=0 if row.get("is_member") is False else 1)
    return list(result.values())


class MembershipCache:
    """Member channels of the operator, refreshed every `refresh_seconds`.

    `client` is a Slack client (anything with `api_call(method, http_verb, params)`) or a
    zero-argument callable returning one, so the service can share the reconcile source's
    client, which exists only after that source starts. A refresh without a client is
    skipped silently.
    """

    def __init__(self, client, store, refresh_seconds=3600, *, policy="exclude", clock=None,
                 configured_channels=()):
        if policy not in POLICIES:
            raise ValueError("Unsupported membership policy")
        self._client = client
        self.store = store
        self.refresh_seconds = refresh_seconds
        self.policy = policy
        self.clock = clock or time.time
        self.configured_channels = tuple(configured_channels)
        self.failures = 0
        self.last_refresh = None
        self.last_attempt = None
        self._members = frozenset()
        self._loaded = False
        self._reported = False
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()

    @property
    def loaded(self):
        return self._loaded

    def client(self):
        client = self._client
        if callable(client) and not hasattr(client, "api_call"):
            client = client()
        return client

    def load(self):
        """Read the ledger table (the previous run's cache); returns True when it has members."""
        rows = self.store.list_membership()
        members = frozenset(row["channel"] for row in rows if row["is_member"])
        if members:
            self._publish(members)
        return bool(members)

    def due(self, now=None):
        now = self.clock() if now is None else now
        return self.last_attempt is None or now < self.last_attempt or now - self.last_attempt >= self.refresh_seconds

    def maybe_refresh(self, *, blocking=True):
        """Refresh when due. A concurrent refresh in progress is waited for or skipped."""
        if not self._lock.acquire(blocking=blocking):
            return False
        try:
            if not self.due():
                return False
            return self._refresh()
        finally:
            self._lock.release()

    def refresh(self):
        with self._lock:
            return self._refresh()

    def _refresh(self):
        client = self.client()
        if client is None:
            return False
        self.last_attempt = self.clock()
        try:
            rows = self._fetch(client)
            if not rows:
                # ok:true with zero channels: keep the previous table instead of an empty set.
                self.failures += 1
                LOG.warning("membership refresh returned no channels; keeping previous table")
                return False
            stamp = datetime.fromtimestamp(self.last_attempt, timezone.utc).astimezone().isoformat()
            self.store.replace_membership(rows, stamp)
        except Exception as error:
            # The previous table and in-memory set stay as they were.
            self.failures += 1
            LOG.warning("Membership refresh failed class=%s error=%s",
                        getattr(error, "error_class", type(error).__name__),
                        getattr(error, "slack_error", None))
            return False
        self.last_refresh = self.last_attempt
        self._publish(frozenset(row["channel"] for row in rows if row["is_member"]))
        return True

    def _fetch(self, client):
        found, page, visited = {}, None, set()
        while True:
            params = dict(types=TYPES, limit=PAGE_LIMIT, exclude_archived=True)
            if page:
                params["cursor"] = page
            try:
                response = client.api_call(METHOD, http_verb="GET", params=params)
            except Exception as error:
                failure = RuntimeError("Slack read request failed")
                failure.error_class = type(error).__name__
                data = getattr(getattr(error, "response", None), "data", None)
                failure.slack_error = (safe(data.get("error"), r"[a-z_]{1,64}")
                                       if isinstance(data, dict) else None)
                raise failure from None
            if not isinstance(response, dict) and hasattr(response, "data"):
                response = response.data
            if not isinstance(response, dict) or not response.get("ok", True):
                failure = RuntimeError("Slack membership response failed")
                failure.slack_error = (safe(response.get("error"), r"[a-z_]{1,64}")
                                       if isinstance(response, dict) else None)
                raise failure
            for row in parse_conversations(response.get("channels", []) or []):
                found[row["channel"]] = row
            page = (response.get("response_metadata") or {}).get("next_cursor")
            if not page:
                return list(found.values())
            if not isinstance(page, str) or page in visited:
                raise RuntimeError("Invalid membership pagination cursor")
            visited.add(page)

    def _publish(self, members):
        with self._state_lock:
            first = not self._loaded and bool(members)
            self._members = members
            self._loaded = self._loaded or bool(members)
        if first:
            LOG.info("Membership cache available member_channels=%d policy=%s", len(members), self.policy)
            self._report_configured()

    def _report_configured(self):
        """Once per process: configured channels that are not member channels."""
        if self._reported:
            return
        self._reported = True
        missing = [channel for channel in sorted(set(self.configured_channels))
                   if member_status(channel, self._members, True) is False]
        if missing:
            LOG.warning("%d configured channels are not member channels: %s (first 5) policy=%s",
                        len(missing), ", ".join(missing[:5]), self.policy)

    def member_channels(self):
        with self._state_lock:
            return set(self._members)

    def is_member(self, channel, channel_type=None):
        """True, False, or None while the cache has never loaded (today's behaviour)."""
        with self._state_lock:
            return member_status(channel, self._members, self._loaded, channel_type)

    def excludes(self, channel, channel_type=None):
        """True when the policy excludes events from this channel."""
        return self.policy == "exclude" and self.is_member(channel, channel_type) is False

    def snapshot(self):
        return {"member_channels": len(self._members)}

