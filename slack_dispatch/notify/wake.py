"""Clock-driven session wake decisions. Durable inbox delivery is independent."""
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Deliver:
    lane: str
    ids: list[str]


@dataclass(frozen=True)
class Defer:
    lane: str
    ids: list[str]
    reason: str = "debounce"


@dataclass(frozen=True)
class StormNotice:
    lane: str
    count: int


@dataclass(frozen=True)
class Batch:
    lane: str
    ids: list[str]
    notice: StormNotice | None = None


class WakePolicyEngine:
    def __init__(self, policy, clock):
        self.policy = policy
        self.clock = clock
        self.events = defaultdict(deque)
        self.last = {}
        self.pending = {}
        self.storm = {}
        self.notices = []

    def setting(self, key, default=None):
        return self.policy.get(key, default) if isinstance(self.policy, dict) else getattr(self.policy, key, default)

    def enabled(self):
        return self.setting("enabled", True) and not (Path(self.setting("state_dir", "state")) / "wake.off").exists()

    def prune(self, lane, now):
        events = self.events[lane]
        while events and events[0] <= now - 3600:
            events.popleft()
        if len(events) < self.setting("storm_per_hour", 30):
            self.storm.pop(lane, None)

    def offer(self, lane, message, author):
        now = self.clock()
        message_id = message.get("id", message.get("message_id"))
        if not message_id:
            raise ValueError("Message id required")
        self.prune(lane, now)
        self.events[lane].append(now)
        if not self.enabled():
            return Defer(lane, [message_id], "disabled")
        if len(self.events[lane]) >= self.setting("storm_per_hour", 30):
            notice = None
            if lane not in self.storm:
                self.storm[lane] = now + self.setting("batch_seconds", 600)
                notice = StormNotice(lane, len(self.events[lane]))
                self.notices.append(notice)
            self.pending[(lane, author, message_id)] = self.storm[lane]
            return Batch(lane, [message_id], notice)
        owner = self.setting("owner_user")
        mentions = message.get("mentions", ()) or ()
        # P36: attention `owner_mention` (an exact mention of the owner or a mention user, or
        # a configured operator name) bypasses the same-author debounce like an exact owner
        # mention; the storm guard above and the global switch still apply.
        bypass = (author in self.setting("immediate_users", ())
                  or message.get("attention") == "owner_mention"
                  or bool(owner and (owner in mentions
                                     or f"<@{owner}>" in (message.get("text") or ""))))
        key = (lane, author)
        last = self.last.get(key)
        if not bypass and last is not None and now < last + self.setting("debounce_seconds", 90):
            self.pending[(lane, author, message_id)] = last + self.setting("debounce_seconds", 90)
            return Defer(lane, [message_id])
        self.last[key] = now
        return Deliver(lane, [message_id])

    def flush(self, now):
        for lane in list(self.events):
            self.prune(lane, now)
        if not self.enabled():
            return []
        ready = defaultdict(list)
        for key, due in list(self.pending.items()):
            lane, author, message_id = key
            if lane in self.storm:
                due = self.storm[lane]
            if due <= now:
                if message_id not in ready[lane]:
                    ready[lane].append(message_id)
                del self.pending[key]
                self.last[(lane, author)] = now
        for lane in ready:
            if lane in self.storm:
                self.storm[lane] = now + self.setting("batch_seconds", 600)
        return list(ready.items())
