"""Validated configuration types. Paths contain no credentials."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Lane:
    id: str
    session: str | None
    paused: bool


@dataclass(frozen=True)
class PersonChannelRule:
    person: str
    channel: str
    lane: str


@dataclass(frozen=True)
class Timers:
    unassigned_seconds: int
    unacknowledged_seconds: int
    reassign_seconds: int


@dataclass(frozen=True)
class WakePolicy:
    mode: str
    enabled: bool
    debounce_seconds: int
    storm_per_hour: int
    batch_seconds: int
    immediate_users: tuple[str, ...] = ()
    owner_user: str | None = None
    state_dir: str = "state"


@dataclass(frozen=True)
class NotifyAdapter:
    kind: str
    path: str | None
    command_template: tuple[str, ...] = ()
    dry_run: bool = False
    timeout: int = 60
    template: dict = field(default_factory=dict)
    purpose: str = "lane"
    # P12 dispatch_card adapters: `path` is the card directory (YAML `dir`).
    only_immediate: bool = True
    workspace_domain: str | None = None


@dataclass(frozen=True)
class SourceConfig:
    kind: str
    enabled: bool
    reconcile_seconds: int
    env_file: str = "dummy.env"
    app_token_key: str = "SLACK_APP_TOKEN"
    user_token_key: str = "SLACK_USER_TOKEN"
    interval_seconds: int = 3600
    lookback_days: int = 7
    store_body: bool = False
    discover_channels: bool = False
    excluded_channels: tuple[str, ...] = ()
    replies: bool = True
    replies_lookback_days: int | None = None
    replies_overlap_seconds: int = 120
    bound_threads: bool = True
    bound_threads_max_age_days: int = 14
    # A reply parent with a permanent Slack error is retried once after this many seconds.
    permanent_error_ttl_seconds: int = 3600


@dataclass(frozen=True)
class LedgerConfig:
    path: str = "state/ledger.sqlite3"
    store_body: bool = False


@dataclass(frozen=True)
class AlwaysHuman:
    users: tuple[str, ...] = ()
    mention_users: tuple[str, ...] = ()
    owner_user: str | None = None


@dataclass(frozen=True)
class Classifier:
    kind: str = "none"
    enabled: bool = False
    model: str = ""
    api_key_env: str = "ANTHROPIC_API_KEY"
    taxonomy_version: str = "v1"
    lane_descriptions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Inbox:
    root: str = "inbox"


@dataclass(frozen=True)
class Service:
    state_dir: str = "state"
    heartbeat_seconds: int = 30
    log_dir: str = "logs"
    log_level: str = "INFO"
    once: bool = False
    followup_window_seconds: int = 600
    gap_alert_seconds: int = 300


@dataclass(frozen=True)
class HumanQueue:
    mode: str = "digest"
    digest_seconds: int = 1800
    immediate_flags: tuple[str, ...] = ("always_human", "owner_mention")


@dataclass(frozen=True)
class BotMessages:
    policy: str = "route_or_exclude"
    known_bot_users: tuple[str, ...] = ()


@dataclass(frozen=True)
class OwnerSignals:
    posts: str = "record"
    reactions_done: tuple[str, ...] = ("white_check_mark", "heavy_check_mark")
    reactions_ack: tuple[str, ...] = ("eyes",)


@dataclass(frozen=True)
class Console:
    """Optional operator console settings (P10). The service reads base_url for notice links."""
    host: str = "127.0.0.1"
    port: int = 8811
    base_url: str | None = None
    page_size: int = 50
    lane_hint_people: bool = True
    # Lane used by the console `direct` action; the action is offered only when configured.
    direct_lane: str = "owner-direct"
    # Optional channel id -> display name map for console listings.
    channels: dict[str, str] = field(default_factory=dict)
    # Console page language (P14): `en` or `ko`.
    ui_language: str = "en"
    # P19: seconds an immediate console action or GO commit stays undoable (at least 10).
    undo_seconds: int = 30


@dataclass(frozen=True)
class Decisions:
    """Card decision consumer (P12). `dir` defaults to the first dispatch_card adapter dir."""
    dir: str | None = None
    poll_seconds: int = 5
    hmac_key_file: str | None = None


@dataclass(frozen=True)
class ScopeLearning:
    """Scope learning (P13): proposals become `suggested` after this many consistent events."""
    min_evidence: int = 2


@dataclass(frozen=True)
class Membership:
    """Member-only intake (P15): `policy` is `exclude` (events from channels the operator
    is not a member of are excluded) or `off`."""
    enabled: bool = True
    refresh_seconds: int = 3600
    policy: str = "exclude"


@dataclass(frozen=True)
class UserDirectory:
    """Bot flag user directory (P16): `users.info` answers cached for `ttl_seconds`.
    With `enabled: false` a message is a bot message only for a missing author, subtype
    `bot_message` or `bot_messages.known_bot_users`."""
    enabled: bool = True
    ttl_seconds: int = 86400
    # After a failed users.info lookup the user is not looked up again for this long.
    failure_cooldown_seconds: int = 300


DEFAULT_CLOSED_STATUSES = ("closed", "answered", "answered_action_ours",
                           "sent_closed_no_action_ours", "delivered")


@dataclass(frozen=True)
class OwnerResolver:
    """Owner resolver and registry sync (P17). With `enabled: false` only the inbox CLI
    `owner` command uses this section, on demand. `registry_path` is the session thread
    registry JSON and `harvest_path` an optional scope harvest jsonl; both are read-only
    local files that only the instance config names."""
    enabled: bool = False
    registry_path: str | None = None
    harvest_path: str | None = None
    refresh_seconds: int = 300
    closed_statuses: tuple[str, ...] = DEFAULT_CLOSED_STATUSES
    close_queue_on_owner_reply: bool = True
    activity_days: int = 60


# P18 delivery log: relay result text patterns (case-insensitive substrings).
DELIVERED_PATTERNS = ('"success":true', "in that session's inbox")
NOT_DELIVERED_PATTERNS = ('"success":false', "ENOINBOX", "no inbox", "could not be delivered",
                          "not found")


@dataclass(frozen=True)
class DeliveryLog:
    """Delivery log (P18): one ledger row per wake or human notice attempt.

    Rows older than `keep_days` are pruned at service start and once a day. The relay
    result text is classified as delivered or not_delivered with these patterns.
    """
    enabled: bool = True
    keep_days: int = 60
    delivered_patterns: tuple[str, ...] = DELIVERED_PATTERNS
    not_delivered_patterns: tuple[str, ...] = NOT_DELIVERED_PATTERNS


@dataclass(frozen=True)
class Config:
    lanes: tuple[Lane, ...]
    channel_defaults: dict[str, str | None]
    dm_defaults: dict[str, str | None]
    person_channel_rules: tuple[PersonChannelRule, ...]
    explicit_tags: dict[str, str]
    excluded_channels: tuple[str, ...]
    timers: Timers
    wake_policy: WakePolicy
    notify_adapters: tuple[NotifyAdapter, ...]
    sources: tuple[SourceConfig, ...]
    ledger: LedgerConfig = LedgerConfig()
    always_human: AlwaysHuman = field(default_factory=AlwaysHuman)
    classifier: Classifier = field(default_factory=Classifier)
    explicit_tag_pattern: str = r"\[(?P<lane>[a-z0-9-]+)\]"
    inbox: Inbox = field(default_factory=Inbox)
    service: Service = field(default_factory=Service)
    human_queue: HumanQueue = field(default_factory=HumanQueue)
    bot_messages: BotMessages = field(default_factory=BotMessages)
    owner_signals: OwnerSignals = field(default_factory=OwnerSignals)
    # Host directory of live session JSON files; expanded at resolution time.
    sessions_dir: str = "~/.claude/sessions"
    # Optional user id -> display name map for the inbox CLI author column.
    people: dict[str, str] = field(default_factory=dict)
    # IANA time zone name for the inbox CLI `when` column.
    display_timezone: str = "UTC"
    # Optional operator console section (P10).
    console: Console = field(default_factory=Console)
    # Optional card decision consumer section (P12).
    decisions: Decisions = field(default_factory=Decisions)
    # Optional scope learning section (P13).
    scope_learning: ScopeLearning = field(default_factory=ScopeLearning)
    # Optional membership section (P15).
    membership: Membership = field(default_factory=Membership)
    # Optional user directory section (P16).
    user_directory: UserDirectory = field(default_factory=UserDirectory)
    # Optional owner resolver section (P17).
    owner_resolver: OwnerResolver = field(default_factory=OwnerResolver)
    # Optional delivery log section (P18).
    delivery_log: DeliveryLog = field(default_factory=DeliveryLog)
