"""Consume card decisions from the card bot (P12).

The card bot writes `<dir>/decisions/<message_id_safe>.json` (`dispatch_decision/1`) when the
operator clicks a lane button on a dispatch card. The service polls that folder every
`decisions.poll_seconds`, verifies each file and applies it through the console action
code (the same path as the console and the inbox CLI):

- a lane id: assign with reason `human:card` (inbox append and a wake request);
- `direct`: assign to `console.direct_lane` when that lane is configured;
- `exclude`: state `excluded` with note `human:card`.

Applied files move to `decisions/done/`, invalid ones to `decisions/rejected/` with one
WARNING naming the file and a reason code. Nothing is deleted, `decisions/.tmp/` is never
read, and neither the key nor any message text is logged. Without a readable key file
the consumer logs once and leaves the decision files untouched.
"""
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re

from slack_dispatch.console import actions
from slack_dispatch.console.queries import direct_lane, inbox_root, lane_ids
from slack_dispatch.notify.dispatch_card import safe_name

LOG = logging.getLogger(__name__)
SCHEMA = "dispatch_decision/1"
KEYS = frozenset({"schema", "message_id", "lane", "decided_by", "decided_at", "card_ts", "hmac"})
REASON = "human:card"
KEY_NAME = "DISPATCH_HMAC_KEY"


class Rejected(Exception):
    """A decision file that can never apply; it moves to `decisions/rejected/`."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def card_dir(config):
    """`decisions.dir`, else the first dispatch_card adapter dir, else None (consumer off)."""
    if config.decisions.dir:
        return config.decisions.dir
    for adapter in config.notify_adapters:
        if adapter.kind == "dispatch_card" and adapter.path:
            return adapter.path
    return None


def canonical(decision):
    """Signed bytes: every key except `hmac`, sorted, compact separators, UTF-8."""
    body = {key: value for key, value in decision.items() if key != "hmac"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign(key, decision):
    return hmac.new(key, canonical(decision), hashlib.sha256).hexdigest()


def read_key(path):
    """Key bytes from a `DISPATCH_HMAC_KEY=<64 hex>` line, or None. Never logged."""
    if not path:
        return None
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        name, separator, value = line.strip().partition("=")
        if separator and name.strip() == KEY_NAME:
            value = value.strip()
            return bytes.fromhex(value) if re.fullmatch(r"[0-9a-fA-F]{64}", value) else None
    return None


class DecisionConsumer:
    def __init__(self, config, store, directory):
        self.config, self.store = config, store
        self.root = Path(directory) / "decisions"
        self.inbox = inbox_root(config)
        self.lanes = set(lane_ids(config))
        self.counters = dict(card_decisions=0, card_decisions_rejected=0)
        self.failures = 0
        self._key_logged = False

    def snapshot(self):
        return dict(self.counters)

    def pending(self):
        """Top-level `decisions/*.json` only; subfolders (.tmp, done, rejected) are skipped."""
        try:
            return sorted(path for path in self.root.glob("*.json")
                          if path.is_file() and not path.name.startswith("."))
        except OSError:
            return []

    def run_once(self):
        """Process every waiting decision file once; return the number applied."""
        files = self.pending()
        if not files:
            return 0
        key = read_key(self.config.decisions.hmac_key_file)
        if key is None:
            if not self._key_logged:
                LOG.warning("Card decisions waiting but the decision key file is missing or "
                            "unreadable; files left in place")
                self._key_logged = True
            return 0
        self._key_logged = False
        applied = 0
        for path in files:
            try:
                decision = self.verify(path, key)
                self.apply(decision)
            except Rejected as rejection:
                self.counters["card_decisions_rejected"] += 1
                LOG.warning("Card decision rejected file=%s reason=%s", path.name, rejection.code)
                self.move(path, "rejected")
                continue
            except Exception as error:
                # Left in place and retried on the next poll.
                self.failures += 1
                LOG.error("Card decision failed file=%s class=%s", path.name, type(error).__name__)
                continue
            self.counters["card_decisions"] += 1
            applied += 1
            self.move(path, "done")
        return applied

    def verify(self, path, key):
        try:
            decision = json.loads(path.read_bytes().decode("utf-8"))
        except (UnicodeError, ValueError):
            raise Rejected("json") from None
        if not isinstance(decision, dict) or set(decision) != KEYS:
            raise Rejected("keys")
        if any(not isinstance(decision[name], str) or not decision[name] for name in KEYS):
            raise Rejected("keys")
        if decision["schema"] != SCHEMA:
            raise Rejected("schema")
        if path.stem != safe_name(decision["message_id"]):
            raise Rejected("file_name")
        if not hmac.compare_digest(sign(key, decision), decision["hmac"]):
            raise Rejected("hmac")
        owner = self.config.always_human.owner_user
        if not owner or decision["decided_by"] != owner:
            raise Rejected("decided_by")
        if (self.root / "done" / path.name).exists():
            raise Rejected("already_decided")
        message = self.store.get_message(decision["message_id"])
        if message is None:
            raise Rejected("unknown_message")
        if actions.inbox_refusal(dict(message, id=decision["message_id"])):
            raise Rejected("not_inbox_item")
        if message["state"] in ("done", "excluded"):
            raise Rejected("closed")
        return decision

    def apply(self, decision):
        message_id, lane = decision["message_id"], decision["lane"]
        try:
            if lane == "exclude":
                actions.exclude(self.store, message_id, reason=REASON)
                return
            target = direct_lane(self.config) if lane == "direct" else lane
            if target is None:
                raise Rejected("direct_lane")
            if target not in self.lanes:
                raise Rejected("lane")
            actions.assign(self.store, self.inbox, message_id, target, reason=REASON)
        except actions.ActionError as error:
            raise Rejected(error.code) from None

    def move(self, path, folder):
        """Move without overwriting: an existing name gets `_1`, `_2`, ... suffixes."""
        try:
            target_dir = self.root / folder
            target_dir.mkdir(parents=True, exist_ok=True)
            target, number = target_dir / path.name, 0
            while target.exists():
                number += 1
                target = target_dir / f"{path.stem}_{number}{path.suffix}"
            os.replace(path, target)
        except OSError as error:
            self.failures += 1
            LOG.error("Card decision move failed file=%s class=%s", path.name, type(error).__name__)
