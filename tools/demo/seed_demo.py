"""Seed a synthetic demo instance for the operator console (offline, no Slack).

    python tools/demo/seed_demo.py --out DIR [--seed N] [--port PORT] [--force]

DIR must be outside this repository. The seed writes DIR/config.yaml, the ledger
DIR/state/ledger.sqlite3 and the small files the console reads: the service heartbeat and
log, the lane inbox files, live session files and a sessions table. Every id, name and
message is fake: channels `C0DEMO...`, users `U0DEMO...`, direct messages `D0DEMO...`,
people named by role and short generic work chatter. Nothing here opens a network
connection or reads a credential.

The ledger is written through the package's own code wherever a writer exists: the router
(`route` and `apply_decision`) places every post, the console action functions (with their
undo journal) make the operator decisions, the inbox CLI functions ack, close and reroute
lane items, `DeliveryRecorder` writes the delivery log and `HeartbeatWriter` the heartbeat.
The store clock is pinned to each scenario moment, so the timestamps those writers record
read like the last 48 hours. Raw SQL is used only for the two service-owned tables the store
has no writer for (`service_deliveries` and `timer_events`). When the schema, the router or
an action changes so that the scenario no longer fits, the seed stops with an error instead
of writing a misleading demo.

Content is deterministic for a given `--seed` (default 2611); timestamps are relative to the
moment the seed runs.
"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    # Running from a clone without installing: the seed must match this repository's code.
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from slack_dispatch import __version__  # noqa: E402
from slack_dispatch.config.loader import load_config  # noqa: E402
from slack_dispatch.console import actions  # noqa: E402
from slack_dispatch.inbox.files import append, record  # noqa: E402
from slack_dispatch.ingest.membership import NOT_MEMBER  # noqa: E402
from slack_dispatch.ledger import store as ledger_store  # noqa: E402
from slack_dispatch.ledger.store import LedgerStore  # noqa: E402
from slack_dispatch.notify.base import DeliveryRecorder  # noqa: E402
from slack_dispatch.pipeline import HUMAN_QUEUE, lane_kind  # noqa: E402
from slack_dispatch.router.apply import apply_decision  # noqa: E402
from slack_dispatch.router.engine import route  # noqa: E402
from slack_dispatch.runtime.heartbeat import HeartbeatWriter  # noqa: E402

DEFAULT_SEED = 2611
DEFAULT_PORT = 8811
MIN_EVIDENCE = 2
UNDO_SECONDS = 30
# The demo heartbeat is written once; a long interval keeps it fresh for three days.
HEARTBEAT_SECONDS = 86400
# Paths the seed owns inside --out. --force moves earlier copies to previous-N/ first.
OWNED = ("config.yaml", "SESSIONS.md", "state", "inbox", "logs", "sessions")
EXIT_REFUSED = 2

# People (role names only). The operator is always_human.owner_user.
OPERATOR = "U0DEMO0001"
OPS_LEAD = "U0DEMO0002"
AGENT_A = "U0DEMO0003"
AGENT_B = "U0DEMO0004"
BILLING = "U0DEMO0005"
DESIGNER = "U0DEMO0006"
WRITER = "U0DEMO0007"
ANALYST = "U0DEMO0008"
PARTNER = "U0DEMO0009"
SPONSOR = "U0DEMO0010"
ALERT_BOT = "U0DEMO0011"
SUCCESS = "U0DEMO0012"
PEOPLE = {
    OPERATOR: "Operator", OPS_LEAD: "Ops Lead", AGENT_A: "Support Agent A",
    AGENT_B: "Support Agent B", BILLING: "Billing Specialist", DESIGNER: "Designer A",
    WRITER: "Writer B", ANALYST: "Data Analyst", PARTNER: "Partner Contact",
    SPONSOR: "Executive Sponsor", ALERT_BOT: "Alert Bot", SUCCESS: "Customer Success",
}
BOTS = (ALERT_BOT,)

# Conversations: (id, membership kind, name, DM partner, operator is a member).
SUPPORT_CH, BILLING_CH, CONTENT_CH = "C0DEMO0001", "C0DEMO0002", "C0DEMO0003"
DATA_CH, OPS_CH, GENERAL_CH = "C0DEMO0004", "C0DEMO0005", "C0DEMO0006"
LEADERSHIP_CH, SOCIAL_CH, DESIGN_CH = "C0DEMO0007", "C0DEMO0008", "C0DEMO0009"
VENDOR_CH = "C0DEMO0010"
DM_OPS_LEAD, DM_PARTNER, DM_BILLING = "D0DEMO0001", "D0DEMO0002", "D0DEMO0003"
CONVERSATIONS = (
    (SUPPORT_CH, "public_channel", "support-inbox", None, True),
    (BILLING_CH, "public_channel", "billing-questions", None, True),
    (CONTENT_CH, "public_channel", "content-review", None, True),
    (DATA_CH, "public_channel", "data-requests", None, True),
    (OPS_CH, "public_channel", "ops-alerts", None, True),
    (GENERAL_CH, "public_channel", "general", None, True),
    (LEADERSHIP_CH, "private_channel", "leadership", None, True),
    (SOCIAL_CH, "public_channel", "social", None, True),
    (DESIGN_CH, "public_channel", "design-crit", None, True),
    (VENDOR_CH, "public_channel", "vendor-shared", None, False),
    (DM_OPS_LEAD, "im", None, OPS_LEAD, True),
    (DM_PARTNER, "im", None, PARTNER, True),
    (DM_BILLING, "im", None, BILLING, True),
)

# Lanes in console order: (id, session value in the config, paused, live session file).
# Session values show each addressing form: none, a full id, a name and an id prefix.
SUPPORT_SESSION = "de000001-0000-4000-8000-00000000d001"
BILLING_SESSION = "de000002-0000-4000-8000-00000000d002"
CONTENT_SESSION = "de000003-0000-4000-8000-00000000d003"
DATA_SESSION = "de000004-0000-4000-8000-00000000d004"
OPS_SESSION = "de000005-0000-4000-8000-00000000d005"
RELAY_SESSION = "de0000ff-0000-4000-8000-00000000d0ff"
DIRECT_LANE = "operator"
LANES = (
    (DIRECT_LANE, None, False),
    ("support", SUPPORT_SESSION, False),
    ("billing", "billing-desk", False),
    ("content", CONTENT_SESSION, True),
    ("data", DATA_SESSION, False),
    ("ops", OPS_SESSION[:8], False),
)
# (lane, session id, live name, title, table state); the data session is not live.
SESSIONS = (
    ("support", SUPPORT_SESSION, "support-desk", "Support desk", "active", True),
    ("billing", BILLING_SESSION, "billing-desk", "Billing desk", "active", True),
    ("content", CONTENT_SESSION, "content-studio", "Content studio", "idle", True),
    ("data", DATA_SESSION, "data-desk", "Data requests", "closed", False),
    ("ops", OPS_SESSION, "ops-oncall", "Ops on-call", "active", True),
)
TARGETS = {lane: (session_id, name) for lane, session_id, name, _, _, _ in SESSIONS}

DELIVERED = "delivered | Message placed in that session's inbox."
NOT_DELIVERED = "not_delivered | The target session has no inbox (ENOINBOX)."
TIMEOUT = "TimeoutExpired: no answer within 60s"
WAKE_STATUS = {"sent": "sent", "not_delivered": "sent", "timeout": "timeout",
               "paused": "skipped_paused", "no_session": "skipped_no_session"}


@dataclass(frozen=True)
class Post:
    """One message of the scenario.

    `expect` is what the router must decide: a lane id, `pending` or `excluded`. `special`
    marks posts the service handles before routing (`owner_post`, `not_member`). Root posts
    are `ago` minutes old; replies come `after` minutes after their `parent`. Each step is a
    tuple whose last element is its delay in minutes after the post:
    (ack, m), (done, m), (wake, status, m), (remind, m), (assign, lane, m),
    (console_done, m), (exclude_channel, m), (reroute, lane, m), (edit, text, m),
    (notice, kind, m).
    """
    key: str
    channel: str
    author: str
    text: str
    expect: str
    ago: int = 0
    parent: str | None = None
    after: int = 0
    steps: tuple = ()
    special: str | None = None
    origin: str = "socket"
    files: bool = False


POSTS = (
    # support: channel default, thread replies, an explicit tag and an owner reply.
    Post("s1", SUPPORT_CH, AGENT_A, "Customer reports the export button times out on large "
         "reports. Can someone take a look?", "support", ago=2760,
         steps=(("wake", "sent", 1), ("ack", 12), ("done", 190))),
    Post("s1r1", SUPPORT_CH, AGENT_B, "Reproduced it with a 10k row report. Steps are in the "
         "ticket.", "support", parent="s1", after=14,
         steps=(("wake", "sent", 1), ("ack", 6), ("done", 176))),
    Post("s2", SUPPORT_CH, SUCCESS, "Two accounts are asking how to reset a workspace "
         "password. Do we have a macro for that?", "support", ago=1200,
         steps=(("wake", "sent", 1), ("ack", 25))),
    Post("s2r1", SUPPORT_CH, AGENT_A, "There is a macro, but it still links to the old help "
         "page.", "support", parent="s2", after=20,
         steps=(("wake", "sent", 1), ("remind", 245))),
    Post("s2r2", SUPPORT_CH, OPERATOR, "Good catch. The new help page goes live tomorrow "
         "morning.", "done", parent="s2", after=45, special="owner_post"),
    Post("s3", SUPPORT_CH, AGENT_B, "New ticket: the mobile app shows a blank screen after "
         "login on older tablets. Screenshot attached.", "support", ago=180, files=True,
         steps=(("wake", "sent", 1),)),
    Post("s4", SUPPORT_CH, SUCCESS, "[support] Customer asked for a copy of last month's "
         "activity log for an audit.", "support", ago=50,
         steps=(("wake", "not_delivered", 1),)),
    # billing: a channel, and a DM routed by a confirmed scope claim.
    Post("b1", BILLING_CH, BILLING, "Invoice totals for the annual plan look off by one seat "
         "on three accounts.", "billing", ago=2400,
         steps=(("wake", "sent", 1), ("ack", 18), ("done", 95))),
    Post("b1r1", BILLING_CH, OPS_LEAD, "Seat count sync ran late on the 1st. Rerunning it "
         "now.", "billing", parent="b1", after=30,
         steps=(("wake", "sent", 1), ("ack", 5), ("done", 64))),
    Post("b2", BILLING_CH, SUCCESS, "Customer wants to switch from monthly to annual billing "
         "mid-cycle. Prorate now or wait for renewal?", "billing", ago=720,
         steps=(("wake", "sent", 1), ("ack", 40))),
    Post("b3", BILLING_CH, BILLING, "Refund request for a duplicate charge. The amount is "
         "under the auto-approve limit.", "billing", ago=120,
         steps=(("wake", "sent", 1),)),
    Post("b4", DM_BILLING, BILLING, "Can you double check the discount code list before I "
         "publish it?", "billing", ago=900,
         steps=(("wake", "sent", 1), ("ack", 9), ("done", 52))),
    # content: the lane is paused, so recent wakes are skipped and items wait.
    Post("c1", CONTENT_CH, DESIGNER, "First draft of the onboarding banner is up for review.",
         "content", ago=1800, steps=(("wake", "sent", 1), ("ack", 30), ("done", 150))),
    Post("c1r1", CONTENT_CH, WRITER, "Copy looks good. Suggest shortening the second line.",
         "content", parent="c1", after=40,
         steps=(("wake", "sent", 1), ("ack", 4), ("done", 110))),
    Post("c2", CONTENT_CH, WRITER, "Blog post outline for the spring release notes is ready "
         "for feedback.", "content", ago=480, steps=(("wake", "paused", 1),)),
    Post("c2r1", CONTENT_CH, DESIGNER, "I can do two header images for it by Thursday.",
         "content", parent="c2", after=35, steps=(("wake", "paused", 1),)),
    Post("c3", CONTENT_CH, DESIGNER, f"<@{OPERATOR}> need your sign-off on the new icon set "
         "before handoff.", "content", ago=60, steps=(("wake", "paused", 1),)),
    Post("k1", DESIGN_CH, DESIGNER, "Posting the revised checkout illustrations for "
         "critique.", "content", ago=1500,
         steps=(("wake", "sent", 1), ("ack", 15), ("done", 80))),
    # data: the lane session is no longer live.
    Post("d1", DATA_CH, ANALYST, "Weekly retention dashboard is refreshed. Numbers are in "
         "the shared folder.", "data", ago=2160, origin="reconcile", steps=(("done", 120),)),
    Post("d2", DATA_CH, OPS_LEAD, "Can we get a breakdown of trial signups by referral "
         "source for last month?", "data", ago=600,
         steps=(("wake", "sent", 1), ("ack", 22))),
    Post("d2r1", DATA_CH, ANALYST, "Query is drafted, waiting on the warehouse refresh.",
         "data", parent="d2", after=55, steps=(("wake", "no_session", 1),)),
    Post("d3", DATA_CH, SUCCESS, "Churn export for the quarterly review, please include plan "
         "tier.", "data", ago=240, steps=(("wake", "no_session", 1),)),
    # ops: bot alerts routed by the channel default, a timeout and a reroute to data.
    Post("o1", OPS_CH, ALERT_BOT, "Disk usage on the staging database crossed 80 percent.",
         "ops", ago=1560, steps=(("wake", "sent", 1), ("ack", 7), ("done", 70))),
    Post("o1r1", OPS_CH, OPS_LEAD, "Expanded the volume. Usage is back to 55 percent.",
         "ops", parent="o1", after=25,
         steps=(("wake", "sent", 1), ("ack", 2), ("done", 45))),
    Post("o2", OPS_CH, ALERT_BOT, "Nightly backup job finished 40 minutes late.", "ops",
         ago=360, steps=(("wake", "timeout", 1), ("wake", "sent", 3), ("ack", 11))),
    Post("o3", OPS_CH, OPS_LEAD, "Planned maintenance window tonight from 22:00 to 23:00 UTC, "
         "heads up.", "ops", ago=30, steps=(("wake", "sent", 1),)),
    Post("o5", OPS_CH, ALERT_BOT, "Warehouse load job failed twice overnight.", "ops",
         ago=420, steps=(("wake", "sent", 1), ("reroute", "data", 26),
                         ("wake", "no_session", 27))),
    # operator (direct lane): a DM default and the mention-only leadership channel.
    Post("m1", DM_OPS_LEAD, OPS_LEAD, "Quick one: are we still on for the vendor review on "
         "Friday?", DIRECT_LANE, ago=300, steps=(("wake", "no_session", 1),)),
    Post("m2", DM_OPS_LEAD, OPS_LEAD, "Shared the budget sheet with you, comments welcome.",
         DIRECT_LANE, ago=1320, steps=(("console_done", 140),)),
    Post("l1", LEADERSHIP_CH, SPONSOR, "The board deck needs the updated hiring plan by "
         "Monday.", DIRECT_LANE, ago=840, steps=(("notice", "attention", 1), ("ack", 35))),
    Post("l2", LEADERSHIP_CH, OPS_LEAD, "Agenda for the planning offsite is in the shared "
         "doc.", "excluded", ago=780),
    Post("l3", LEADERSHIP_CH, WRITER, f"<@{OPERATOR}> can you approve the press kit wording "
         "today?", DIRECT_LANE, ago=150,
         steps=(("wake", "no_session", 1), ("notice", "attention", 2))),
    Post("l4", LEADERSHIP_CH, DESIGNER, "Lunch order for the offsite is sorted.", "excluded",
         ago=1080),
    # general: no lane default, so posts wait for the operator (human_pending).
    Post("g0", GENERAL_CH, SUCCESS, "Can someone update the pricing FAQ with the new seat "
         "limits?", "pending", ago=1980,
         steps=(("assign", "content", 42), ("wake", "sent", 43), ("ack", 60), ("done", 200))),
    Post("g0b", GENERAL_CH, WRITER, "Draft of the holiday hours announcement is ready. Who "
         "reviews it?", "pending", ago=1620,
         steps=(("assign", "content", 18), ("wake", "sent", 19), ("ack", 25), ("done", 90))),
    Post("g1a", DM_PARTNER, PARTNER, "Could you send over the updated API rate limits "
         "sheet?", "pending", ago=2280,
         steps=(("assign", "support", 30), ("wake", "sent", 31), ("ack", 40),
                ("done", 120))),
    Post("g1", DM_PARTNER, PARTNER, "Hi, following up on the integration questions from last "
         "week.", "pending", ago=540),
    Post("g2", GENERAL_CH, SUCCESS, "Who owns the status page copy now? It still lists the "
         "old support hours.", "pending", ago=420,
         steps=(("edit", "Who owns the status page copy now? It still lists the old support "
                 "hours and the old phone line.", 6),)),
    Post("g5", GENERAL_CH, AGENT_B, "I think it moved to the content team last month.",
         "pending", parent="g2", after=380),
    Post("g3", GENERAL_CH, SPONSOR, "Please send me a one-paragraph summary of this week's "
         "incidents.", "pending", ago=270),
    Post("g4", GENERAL_CH, ANALYST, f"<@{OPERATOR}> do we want the new metrics in the "
         "monthly report or the quarterly one?", "pending", ago=90,
         steps=(("notice", "card", 1),)),
    # excluded: a channel the operator excluded, a bot post, a non-member channel.
    Post("x1", SOCIAL_CH, DESIGNER, "Photos from the team lunch are up in the shared album.",
         "pending", ago=1680, steps=(("exclude_channel", 25),)),
    Post("x2", GENERAL_CH, ALERT_BOT, "Weekly uptime report: 99.95 percent across all "
         "services.", "excluded", ago=960),
    Post("x3", VENDOR_CH, PARTNER, "Updated the shared roadmap doc with the new milestones.",
         "excluded", ago=660, special="not_member"),
)
# Human queue digests: (post keys covered, anchor post, minutes after the anchor).
DIGESTS = ((("g1", "g2"), "g2", 25),)
# Scope claims confirmed before the window: (lane, kind, key, note).
CLAIMS = (("content", "channel", DESIGN_CH, "design critique goes to content"),
          ("billing", "dm", DM_BILLING, "billing specialist DMs"))


class SeedError(Exception):
    """A refusal shown to the person running the seed (exit code 2)."""


def iso(epoch):
    """Local ISO time with offset, the format the ledger store writes."""
    return datetime.fromtimestamp(float(epoch), timezone.utc).astimezone().isoformat()


def utc_iso(epoch):
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


class Clock:
    """The scenario moment; the ledger store's own `now()` reads it while seeding."""

    def __init__(self, epoch):
        self.epoch = float(epoch)

    def iso(self):
        return iso(self.epoch)

    def __call__(self):
        return self.epoch


@contextmanager
def pinned_store_clock(clock):
    """Point `slack_dispatch.ledger.store.now` at the scenario clock for the seeding run."""
    original = ledger_store.now
    ledger_store.now = clock.iso
    try:
        yield
    finally:
        ledger_store.now = original


def check_out_dir(out, force):
    """Resolve and validate --out; refuse a path inside the repository, a directory that
    contains it, or a non-empty directory without --force. Nothing is created before the
    checks pass."""
    path = Path(out).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise SeedError(f"Refusing to write inside the repository ({ROOT}). "
                        "Choose an --out directory outside it.")
    if path in ROOT.parents:
        raise SeedError(f"Refusing to write into a directory that contains the repository "
                        f"({ROOT}). Choose an --out directory outside it.")
    if path.exists() and not path.is_dir():
        raise SeedError(f"--out exists and is not a directory: {path}")
    if path.exists() and any(path.iterdir()) and not force:
        raise SeedError(f"--out is not empty: {path}. Use an empty directory, or --force to "
                        "move an earlier demo's files to previous-N/ first.")
    return path


def move_previous(out):
    """--force: move the paths an earlier run wrote into previous-N/ (nothing is deleted)."""
    present = [name for name in OWNED if (out / name).exists()]
    if not present:
        return None
    number = 1
    while (out / f"previous-{number}").exists():
        number += 1
    target = out / f"previous-{number}"
    target.mkdir()
    for name in present:
        (out / name).rename(target / name)
    return target


def config_data(out, port):
    """The demo instance configuration (absolute paths, dummy ids only)."""
    state, inbox = out / "state", out / "inbox"
    return {
        "lanes": [{"id": lane, "session": session, "paused": paused}
                  for lane, session, paused in LANES],
        "sessions_dir": (out / "sessions").as_posix(),
        "people": {OPERATOR: PEOPLE[OPERATOR]},
        "display_timezone": "UTC",
        "channel_defaults": {
            SUPPORT_CH: {"default": "support"},
            BILLING_CH: {"default": "billing"},
            CONTENT_CH: {"default": "content"},
            DATA_CH: {"default": "data"},
            OPS_CH: {"default": "ops"},
            GENERAL_CH: {"default": None},
            LEADERSHIP_CH: {"default": DIRECT_LANE, "mention_only": True},
        },
        "dm_defaults": {DM_OPS_LEAD: {"default": DIRECT_LANE}},
        "person_channel_rules": [{"person": DESIGNER, "channel": GENERAL_CH, "lane": "content"}],
        "explicit_tags": {f"[{lane}]": lane for lane, _, _ in LANES if lane != DIRECT_LANE},
        "excluded_channels": [],
        "timers": {"unassigned_seconds": 1800, "unacknowledged_seconds": 14400,
                   "reassign_seconds": 86400},
        "wake_policy": {"mode": "immediate", "enabled": True, "debounce_seconds": 90,
                        "storm_per_hour": 30, "batch_seconds": 600, "owner_user": OPERATOR,
                        "followup": "changed_after_ack", "catchup_minutes": 30},
        "inbox": {"root": inbox.as_posix()},
        # Every adapter is a dry run: even a service started on this config sends nothing.
        "notify_adapters": [
            {"kind": "file_inbox", "path": inbox.as_posix()},
            {"kind": "session_message", "dry_run": True, "timeout": 60,
             "command_template": ["demo-session-cli", "--target", "{target}",
                                  "--message", "{message}"]},
            {"kind": "session_message", "purpose": "human_queue", "dry_run": True,
             "command_template": ["demo-session-cli", "--target", "{target}",
                                  "--message", "{message}"],
             "template": {"target": "operator"}},
            {"kind": "dispatch_card", "purpose": "human_queue", "only_immediate": True,
             "dir": (state / "dispatch_cards").as_posix()},
        ],
        "ledger": {"path": (state / "ledger.sqlite3").as_posix(), "store_body": True},
        "sources": [],
        "always_human": {"users": [SPONSOR], "mention_users": [], "owner_user": OPERATOR},
        "classifier": {"kind": "none", "enabled": False},
        "service": {"state_dir": state.as_posix(), "heartbeat_seconds": HEARTBEAT_SECONDS,
                    "log_dir": (out / "logs").as_posix(), "log_level": "INFO"},
        "human_queue": {"mode": "digest", "digest_seconds": 1800,
                        "immediate_flags": ["always_human", "owner_mention"]},
        "bot_messages": {"policy": "route_or_exclude", "known_bot_users": list(BOTS)},
        "console": {"host": "127.0.0.1", "port": port, "page_size": 50,
                    "lane_hint_people": True, "direct_lane": DIRECT_LANE, "ui_language": "en",
                    "undo_seconds": UNDO_SECONDS, "workspace_domain": None,
                    "sessions_table": (out / "SESSIONS.md").as_posix(), "channels": {}},
        "scope_learning": {"min_evidence": MIN_EVIDENCE},
        "membership": {"enabled": True, "refresh_seconds": 3600, "policy": "exclude"},
        "user_directory": {"enabled": True, "resolve_names": True},
        "owner_resolver": {"enabled": False},
        "delivery_log": {"enabled": True, "keep_days": 60},
    }


def write_config(out, port):
    header = ("# slack-dispatch demo instance written by tools/demo/seed_demo.py.\n"
              "# Synthetic data only: dummy ids, role names, no tokens and no Slack source.\n")
    path = out / "config.yaml"
    path.write_text(header + yaml.safe_dump(config_data(out, port), sort_keys=False),
                    encoding="utf-8", newline="\n")
    return path


def write_sessions(out, now):
    """Live session files (the data session is left out, so it reads as not live) and the
    Markdown sessions table that names lane sessions by title."""
    sessions = out / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    rows = ["| Last used | State | Title | Size | Resume |", "| --- | --- | --- | --- | --- |"]
    for index, (_lane, session_id, name, title, state, live) in enumerate(SESSIONS):
        if live:
            (sessions / f"{name}.json").write_text(
                json.dumps({"sessionId": session_id, "name": name}) + "\n", encoding="utf-8",
                newline="\n")
        used = datetime.fromtimestamp(now - 600 * (index + 1), timezone.utc)
        rows.append(f"| {used:%Y-%m-%d %H:%M} | {state} | {title} | {index + 1}.{index} MB "
                    f"| `claude --resume {session_id}` |")
    (out / "SESSIONS.md").write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")


def build_timeline(rng, now):
    """(epoch, order, post, step) entries in time order; times jittered by the seed."""
    times, entries = {}, []
    for post in POSTS:
        if post.parent:
            moment = times[post.parent] + post.after * 60 + rng.randint(5, 50)
        else:
            moment = now - post.ago * 60 + rng.randint(-240, 240)
        moment = min(int(moment), int(now) - 120)
        times[post.key] = moment
        entries.append((moment, len(entries), post, None))
        for step in post.steps:
            at = min(moment + step[-1] * 60 + rng.randint(0, 40), int(now) - 30)
            entries.append((at, len(entries), post, step))
    for keys, anchor, minutes in DIGESTS:
        at = min(times[anchor] + minutes * 60 + rng.randint(0, 40), int(now) - 30)
        entries.append((at, len(entries), None, ("digest", keys)))
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    return entries, times


class Seeder:
    """Applies the scenario to an open ledger through the package's own writers."""

    def __init__(self, store, config, out, rng, clock):
        self.store, self.config, self.out, self.rng, self.clock = store, config, out, rng, clock
        self.inbox = Path(config.inbox.root)
        self.ids = {}
        self.events = 0
        self.log = []  # (epoch, level, logger, message) for the service log
        self.lane_recorder = DeliveryRecorder(store, purpose="lane", adapter="session_message",
                                              clock=clock)
        self.human_recorder = DeliveryRecorder(store, purpose=HUMAN_QUEUE,
                                               adapter="session_message", clock=clock)
        self.card_recorder = DeliveryRecorder(store, purpose=HUMAN_QUEUE,
                                              adapter="dispatch_card", clock=clock)

    # Setup before the scenario window.

    def setup(self, start):
        store = self.store
        for lane, _, _ in LANES:
            store.upsert_lane(lane)
        for user, name in PEOPLE.items():
            store.put_user_directory({"user": user, "name": name, "is_bot": user in BOTS,
                                      "is_app_user": user in BOTS, "deleted": False}, iso(start))
        store.replace_membership([{"channel": channel, "kind": kind, "name": name,
                                   "partner_user": partner, "is_member": member}
                                  for channel, kind, name, partner, member in CONVERSATIONS],
                                 iso(start))
        for lane, kind, key, note in CLAIMS:
            self.clock.epoch = start
            store.add_scope_claim(lane, kind, key, note=note, claimed_by="cli")
            self.clock.epoch = start + 300
            store.confirm_scope_claim(lane, kind, key, by="console")
        gap = store.record_gap("service", iso(start), None, iso(start), "start")
        store.complete_gaps(iso(start + 90))
        self.log.append((start, "INFO", "slack_dispatch.service",
                         f"Ingest gap id={gap} source=service"))
        with store._transaction() as db:
            # The service-owned table (pipeline.Pipeline creates it the same way).
            db.execute("""CREATE TABLE IF NOT EXISTS service_deliveries (
                source TEXT NOT NULL, channel TEXT NOT NULL, thread_ts TEXT NOT NULL,
                lane TEXT NOT NULL, delivered_at REAL NOT NULL,
                PRIMARY KEY (source, channel, thread_ts, lane))""")

    # Posts and steps.

    def post(self, post, moment, parent_ts):
        self.events += 1
        index = len(self.ids) + 1
        ts = f"{moment}.{index * 100:06d}"
        event = {"source": "slack", "event_id": f"EvDEMO{self.events:04d}",
                 "event_type": "message", "channel": post.channel,
                 "channel_type": "im" if post.channel.startswith("D") else "channel",
                 "user": post.author, "ts": ts, "thread_ts": parent_ts,
                 "is_thread_reply": bool(parent_ts), "has_files": post.files,
                 "received_at": iso(moment + 2), "text": post.text,
                 "is_bot": post.author in BOTS, "origin": post.origin}
        result = self.store.record_event(event)
        if not result.created:
            raise RuntimeError(f"Demo post {post.key} was not recorded")
        mid = result.message_id
        self.ids[post.key] = mid
        self.clock.epoch = moment + 20
        if post.special == "owner_post":
            # The pipeline records owner posts before routing (owner_signals.posts record).
            self.store.set_route(mid, None, "owner_post", state="done")
            return
        if post.special == "not_member":
            self.store.set_state(mid, "excluded", note=NOT_MEMBER)
            return
        message = dict(self.store.get_message(mid, include_body=True), text=post.text,
                       user=post.author)
        outcome = apply_decision(self.store, mid, route(message, self.config,
                                                        self.store.lookup_binding), None)
        expected = {"pending": ("human_pending", None), "excluded": ("excluded", None)}.get(
            post.expect, ("assigned", post.expect))
        if (outcome.state, outcome.lane) != expected:
            raise RuntimeError(f"Demo post {post.key} routed to {outcome.state}/{outcome.lane}, "
                               f"expected {expected[0]}/{expected[1]}: the scenario no longer "
                               "matches the router")
        if outcome.lane:
            row = record(dict(self.store.get_message(mid), id=mid))
            row["delivered_at"] = utc_iso(moment + 30)
            append(self.inbox, outcome.lane, [row])

    def step(self, post, step, at):
        if step[0] == "digest":
            self.step_digest(at, step[1])
            return
        getattr(self, "step_" + step[0])(self.ids[post.key], at, *step[1:-1])

    def step_ack(self, mid, at):
        actions.ack(self.store, mid)

    def step_done(self, mid, at):
        actions.done(self.store, mid)

    def step_edit(self, mid, at, text):
        message = self.store.get_message(mid)
        self.events += 1
        self.store.record_event({"source": "slack", "event_id": f"EvDEMO{self.events:04d}",
                                 "event_type": "message", "subtype": "message_changed",
                                 "channel": message["channel"], "ts": message["ts"],
                                 "user": message["author"], "is_bot": False, "text": text,
                                 "received_at": iso(at)})

    def recorded(self, row_id, what):
        if row_id is None:
            raise RuntimeError(f"Delivery log write failed for {what}")
        return row_id

    def step_wake(self, mid, at, status):
        message = self.store.get_message(mid)
        lane = message["lane"]
        fields = {"kind": lane_kind(self.store, [mid]), "at": at}
        if status in ("sent", "not_delivered", "timeout"):
            session_id, name = TARGETS[lane]
            fields.update(target_session_id=session_id, target_session_name=name)
        if status in ("sent", "not_delivered"):
            fields.update(detail=DELIVERED if status == "sent" else NOT_DELIVERED,
                          cost_usd=round(self.rng.uniform(0.01, 0.06), 4),
                          duration_ms=self.rng.randint(4000, 15000),
                          relay_session_id=RELAY_SESSION)
            self.service_delivery(message, lane, at)
        elif status == "timeout":
            fields["detail"] = TIMEOUT
            self.log.append((at + 120, "ERROR", "slack_dispatch.pipeline",
                             "Pipeline exception operation=notify class=NotifyError"))
        elif status == "no_session" and lane != DIRECT_LANE:
            self.log.append((at, "INFO", "slack_dispatch.notify.session_resolve",
                             f"session not live: lane {lane}"))
        self.recorded(self.lane_recorder.record(lane, [mid], WAKE_STATUS[status], **fields),
                      f"lane {lane}")

    def step_remind(self, mid, at):
        """The unacknowledged timer: one timer_events row and a reminder wake."""
        message = self.store.get_message(mid)
        with self.store._transaction() as db:
            db.execute("INSERT OR IGNORE INTO timer_events VALUES (?, ?, ?, ?)",
                       (mid, "unacknowledged", 1, float(at)))
        session_id, name = TARGETS[message["lane"]]
        self.recorded(self.lane_recorder.record(
            message["lane"], [mid], "sent", kind="reminder", detail=DELIVERED,
            target_session_id=session_id, target_session_name=name, at=at,
            cost_usd=round(self.rng.uniform(0.01, 0.06), 4),
            duration_ms=self.rng.randint(4000, 15000), relay_session_id=RELAY_SESSION),
            "reminder")

    def service_delivery(self, message, lane, at):
        with self.store._transaction() as db:
            db.execute("""INSERT INTO service_deliveries VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, channel, thread_ts, lane)
                DO UPDATE SET delivered_at=excluded.delivered_at""",
                       (message["source"], message["channel"],
                        message.get("thread_ts") or message["ts"], lane, float(at)))

    def console(self, action, mid, at, lane=None):
        """One operator decision through the console action code, journaled for undo."""
        journal = actions.Journal(self.store, action, lane=lane)
        result = actions.apply(self.store, str(self.inbox), action, [mid], lane=lane,
                               guard=actions.operator_guard(DIRECT_LANE),
                               min_evidence=MIN_EVIDENCE, journal=journal,
                               direct_lane=DIRECT_LANE)[0]
        if not result["ok"]:
            raise RuntimeError(f"Demo console {action} failed: {result['error']}")
        if journal.finish(UNDO_SECONDS, at) is None:
            raise RuntimeError(f"Demo console {action} was not journaled")
        self.consume_wakes(at)

    def consume_wakes(self, at):
        """The service consumes queued wake requests within seconds."""
        pending = [row["id"] for row in self.store.pending_wake_requests()]
        if pending:
            self.store.consume_wake_requests(pending, at=iso(at + 5))

    def step_assign(self, mid, at, lane):
        self.console("assign", mid, at, lane)

    def step_console_done(self, mid, at):
        self.console("done", mid, at)

    def step_exclude_channel(self, mid, at):
        self.console("exclude_channel", mid, at)

    def step_reroute(self, mid, at, lane):
        actions.reroute(self.store, str(self.inbox), mid, lane, source="cli",
                        min_evidence=MIN_EVIDENCE)
        self.consume_wakes(at)

    def step_notice(self, mid, at, kind):
        if kind == "card":
            row = self.card_recorder.record(HUMAN_QUEUE, [mid], "sent", kind="card",
                                            detail="cards written 1", at=at)
        else:
            row = self.human_recorder.record(HUMAN_QUEUE, [mid], "sent", kind=kind,
                                             detail=DELIVERED, target_session_name="operator",
                                             at=at)
        self.recorded(row, "human notice")

    def step_digest(self, at, keys):
        ids = [self.ids[key] for key in keys]
        self.recorded(self.human_recorder.record(HUMAN_QUEUE, ids, "sent", kind="digest",
                                                 detail=DELIVERED,
                                                 target_session_name="operator", at=at),
                      "digest")


def write_service_files(seeder, out, now, start, latest):
    """Heartbeat (through HeartbeatWriter) and a service log the Service tab parses."""
    store = seeder.store
    gap_start = now - 18 * 3600
    gap = store.record_gap("slack_socket", iso(gap_start), iso(gap_start - 360),
                           iso(gap_start + 240), "reconnect")
    store.complete_gaps(iso(gap_start + 420))
    seeder.log.append((gap_start + 240, "INFO", "slack_dispatch.service",
                       f"Ingest gap id={gap} source=slack_socket"))
    seeder.log.append((now - 9 * 3600, "WARNING", "slack_dispatch.ingest.slack_reconcile",
                       f"Reconcile replies failed channel={DATA_CH} parent=- class=SlackApiError "
                       "error=ratelimited"))
    hour = start + 1800
    while hour < now - 60:
        seeder.log.append((hour, "INFO", "slack_dispatch.ingest.slack_reconcile",
                           "Reconcile pass channels=10 roots=2 parents=1 replies=1 hydrated=0 "
                           "duplicates=3 prehandled=0 failures=0 thread_candidates=2 "
                           "thread_skipped_age=0 permanent_skipped=0; backfill delivered 0 "
                           "(no wake); catchup_wakes=0"))
        hour += 3600
    logs = out / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    lines = []
    for epoch, level, logger, message in sorted(seeder.log, key=lambda entry: entry[0]):
        moment = datetime.fromtimestamp(epoch)
        lines.append(f"{moment:%Y-%m-%d %H:%M:%S},{moment.microsecond // 1000:03d} "
                     f"{level} {logger} {message}")
    (logs / "service.log").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    rows = store.list_messages(limit=1000)
    states = [row["state"] for row in rows]
    deliveries = store.list_deliveries()
    counters = {"events": seeder.events, "routed": sum(bool(row["lane"]) for row in rows),
                "human_pending": states.count("human_pending"),
                "delivered": sum(row["status"] == "sent" for row in deliveries),
                "failures": sum(row["status"] in ("failed", "timeout") for row in deliveries),
                "duplicates": 3, "prehandled": 0, "hydrated": 0,
                "excluded_no_mention": sum(row["note"] == "excluded:no_mention" for row in rows)}
    writer = HeartbeatWriter(Path(seeder.config.service.state_dir) / "heartbeat.json",
                             interval_seconds=HEARTBEAT_SECONDS, clock=lambda: now)
    writer.write(ok=True, status="running", version=__version__, counters=counters, force=True,
                 started_at=iso(start), last_event_at=iso(latest),
                 last_catchup_at=iso(now - 1500), gaps=2)


def seed(out, *, seed_value=DEFAULT_SEED, port=DEFAULT_PORT, force=False, now=None):
    """Write the demo instance into `out` and return a summary dict. Raises SeedError for a
    refused --out and RuntimeError when the scenario no longer fits the package."""
    out = check_out_dir(out, force)
    now = int(time.time() if now is None else now)
    out.mkdir(parents=True, exist_ok=True)
    moved = move_previous(out) if force else None
    (out / "state").mkdir(parents=True, exist_ok=True)
    (out / "inbox").mkdir(parents=True, exist_ok=True)
    config_path = write_config(out, port)
    config = load_config(config_path)
    write_sessions(out, now)
    rng = random.Random(seed_value)
    entries, times = build_timeline(rng, now)
    start = now - 47 * 3600 - 48 * 60
    clock = Clock(start)
    store = LedgerStore(config.ledger.path, store_body=config.ledger.store_body)
    store.open()
    try:
        store.migrate()
        seeder = Seeder(store, config, out, rng, clock)
        with pinned_store_clock(clock):
            seeder.setup(start)
            for at, _, post, step in entries:
                clock.epoch = at
                if step is None:
                    parent_ts = None
                    if post.parent:
                        parent_ts = store.get_message(seeder.ids[post.parent])["ts"]
                    seeder.post(post, at, parent_ts)
                else:
                    seeder.step(post, step, at)
        write_service_files(seeder, out, now, start, max(times.values()))
        rows = store.list_messages(limit=1000)
        summary = {
            "out": str(out), "config": str(config_path), "ledger": str(Path(config.ledger.path)),
            "seed": seed_value, "messages": len(rows),
            "human_pending": sum(row["state"] == "human_pending" for row in rows),
            "deliveries": store.count_deliveries(), "lanes": len(LANES),
            "previous": str(moved) if moved else None,
            "command": f'python -m slack_dispatch.console --config "{config_path}"',
            "url": f"http://127.0.0.1:{config.console.port}/",
        }
    finally:
        store.close()
    return summary


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python tools/demo/seed_demo.py",
        description="Write a synthetic slack-dispatch instance (config and ledger) for the "
                    "operator console. Offline: nothing talks to Slack.")
    parser.add_argument("--out", required=True, metavar="DIR",
                        help="target directory outside the repository (created when missing)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help=f"scenario seed for timing jitter (default {DEFAULT_SEED})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"console port written to the config (default {DEFAULT_PORT})")
    parser.add_argument("--force", action="store_true",
                        help="allow a non-empty directory; an earlier demo's files are moved "
                             "to previous-N/ first (nothing is deleted)")
    return parser


def main(argv=None, *, stdout=None, stderr=None, now=None):
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        print("--port must be from 1 to 65535", file=stderr)
        return EXIT_REFUSED
    try:
        summary = seed(args.out, seed_value=args.seed, port=args.port, force=args.force, now=now)
    except SeedError as error:
        print(str(error), file=stderr)
        return EXIT_REFUSED
    if summary["previous"]:
        print(f"Moved the earlier demo files to {summary['previous']}", file=stdout)
    print(f"Demo instance written to {summary['out']}", file=stdout)
    print(f"  config: {summary['config']}", file=stdout)
    print(f"  ledger: {summary['ledger']} ({summary['messages']} messages, "
          f"{summary['human_pending']} waiting for the operator, "
          f"{summary['deliveries']} delivery log rows, seed {summary['seed']})", file=stdout)
    print("Everything is synthetic (C0DEMO/U0DEMO/D0DEMO ids, role names); nothing talks to "
          "Slack.", file=stdout)
    print("Start the console (needs the console extra: pip install -e .[console]):", file=stdout)
    print(f"  {summary['command']}", file=stdout)
    print(f"It binds to 127.0.0.1 only. Open {summary['url']}", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
