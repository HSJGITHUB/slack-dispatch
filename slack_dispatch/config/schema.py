"""Validated configuration types. Paths contain no credentials."""
from dataclasses import dataclass


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


@dataclass(frozen=True)
class NotifyAdapter:
    kind: str
    path: str | None


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
