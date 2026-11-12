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
