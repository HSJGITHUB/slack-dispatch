"""Load YAML only; never read credentials, expand environment, or start sources."""
from pathlib import Path
import re

import yaml

from .schema import (
    AlwaysHuman, Classifier, Config, Inbox, Lane, LedgerConfig, NotifyAdapter,
    PersonChannelRule, Service, SourceConfig, Timers, WakePolicy,
)


class ConfigError(ValueError):
    """A configuration error safe to display without raw input values."""


def mapping(value, path):
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping")
    return value


def required(data, key, path):
    if key not in data:
        raise ConfigError(f"{path}.{key} is required")
    return data[key]


def string(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} must be a non-empty string")
    return value


def boolean(value, path):
    if type(value) is not bool:
        raise ConfigError(f"{path} must be a boolean")
    return value


def positive(value, path):
    if type(value) is not int or value <= 0:
        raise ConfigError(f"{path} must be a positive integer")
    return value


def parse_config(raw):
    data = mapping(raw, "config")

    def get(key):
        return required(data, key, "config")

    def rows(key):
        value = get(key)
        if not isinstance(value, list):
            raise ConfigError(f"config.{key} must be a list")
        return value

    lanes = []
    for row in rows("lanes"):
        row = mapping(row, "lanes entry")
        lane_id = string(required(row, "id", "lane"), "lane.id")
        if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in lane_id):
            raise ConfigError("lane.id must use lowercase letters, digits, hyphens or underscores")
        session = row.get("session")
        if session is not None:
            string(session, "lane.session")
        lanes.append(Lane(lane_id, session, boolean(row.get("paused", False), "lane.paused")))
    ids = {lane.id for lane in lanes}
    if not lanes or len(ids) != len(lanes):
        raise ConfigError("lanes must contain unique ids and at least one lane")

    def lane_ref(value, path, nullable=False):
        if nullable and value is None:
            return None
        string(value, path)
        if value not in ids:
            raise ConfigError(f"{path} must reference a configured lane")
        return value

    def defaults(key):
        result = {}
        for channel, entry in mapping(get(key), key).items():
            string(channel, key + " key")
            entry = mapping(entry, key + " entry")
            result[channel] = lane_ref(required(entry, "default", key), key, True)
        return result

    rules = []
    for row in rows("person_channel_rules"):
        row = mapping(row, "person_channel_rules entry")
        rules.append(PersonChannelRule(
            string(required(row, "person", "rule"), "rule.person"),
            string(required(row, "channel", "rule"), "rule.channel"),
            lane_ref(required(row, "lane", "rule"), "rule.lane"),
        ))
    tags = {string(k, "explicit_tags key"): lane_ref(v, "explicit_tags lane")
            for k, v in mapping(get("explicit_tags"), "explicit_tags").items()}
    excluded = tuple(string(v, "excluded_channels entry") for v in rows("excluded_channels"))
    timers = mapping(get("timers"), "timers")
    timers = Timers(*(positive(required(timers, k, "timers"), "timers." + k)
                      for k in ("unassigned_seconds", "unacknowledged_seconds", "reassign_seconds")))
    if timers.reassign_seconds <= timers.unacknowledged_seconds:
        raise ConfigError("timers.reassign_seconds must exceed unacknowledged_seconds")
    wake = mapping(get("wake_policy"), "wake_policy")
    if required(wake, "mode", "wake_policy") != "immediate":
        raise ConfigError("wake_policy.mode must be immediate")
    immediate_users = wake.get("immediate_users", [])
    if not isinstance(immediate_users, list):
        raise ConfigError("wake_policy.immediate_users must be a list")
    immediate_users = tuple(string(v, "wake_policy.immediate_users entry") for v in immediate_users)
    owner_user = wake.get("owner_user")
    if owner_user is not None:
        string(owner_user, "wake_policy.owner_user")
    wake = WakePolicy("immediate", boolean(required(wake, "enabled", "wake_policy"), "wake_policy.enabled"),
                      *(positive(required(wake, k, "wake_policy"), "wake_policy." + k)
                        for k in ("debounce_seconds", "storm_per_hour", "batch_seconds")),
                      immediate_users=immediate_users, owner_user=owner_user,
                      state_dir=string(wake.get("state_dir", "state"), "wake_policy.state_dir"))
    inbox = mapping(data.get("inbox", {"root": "inbox"}), "inbox")
    inbox = Inbox(string(required(inbox, "root", "inbox"), "inbox.root"))
    adapters = []
    for row in rows("notify_adapters"):
        row = mapping(row, "notify_adapters entry")
        kind = required(row, "kind", "notify_adapter")
        if kind not in ("file_inbox", "session_message", "assistant_dm", "file_drop"):
            raise ConfigError("notify_adapter.kind is unsupported")
        path = row.get("outbox_dir", row.get("path", inbox.root if kind == "file_inbox" else None))
        if kind != "session_message" or path is not None:
            string(path, "notify_adapter.path")
        command = row.get("command_template", [])
        if not isinstance(command, list) or any(not isinstance(v, str) or not v for v in command):
            raise ConfigError("notify_adapter.command_template must be a string list")
        template = mapping(row.get("template", {"lane": "{lane}", "ids": "{ids}", "count": "{count}"}), "notify_adapter.template")
        purpose = row.get("purpose", row.get("for", "lane"))
        if purpose not in ("lane", "human_queue"):
            raise ConfigError("notify_adapter.purpose must be lane or human_queue")
        adapters.append(NotifyAdapter(kind, path, tuple(command),
                                      boolean(row.get("dry_run", False), "notify_adapter.dry_run"),
                                      positive(row.get("timeout", 60), "notify_adapter.timeout"),
                                      template, purpose))
    ledger_data = mapping(get("ledger"), "ledger")
    ledger = LedgerConfig(
        string(required(ledger_data, "path", "ledger"), "ledger.path"),
        boolean(ledger_data.get("store_body", False), "ledger.store_body"),
    )
    sources = []
    for row in rows("sources"):
        row = mapping(row, "sources entry")
        kind = required(row, "kind", "source")
        if kind not in ("slack_socket", "slack_reconcile"):
            raise ConfigError("source.kind is unsupported")
        sources.append(SourceConfig(kind, boolean(required(row, "enabled", "source"), "source.enabled"),
                                    positive(row.get("reconcile_seconds", 3600), "source.reconcile_seconds"),
                                    string(row.get("env_file", "dummy.env"), "source.env_file"),
                                    string(row.get("app_token_key", "SLACK_APP_TOKEN"), "source.app_token_key"),
                                    string(row.get("user_token_key", "SLACK_USER_TOKEN"), "source.user_token_key"),
                                    positive(row.get("interval_seconds", row.get("reconcile_seconds", 3600)), "source.interval_seconds"),
                                    positive(row.get("lookback_days", 7), "source.lookback_days"),
                                    ledger.store_body))
    human = mapping(data.get("always_human", {}), "always_human")
    def user_ids(key):
        values = human.get(key, [])
        if not isinstance(values, list):
            raise ConfigError(f"always_human.{key} must be a list")
        return tuple(string(value, f"always_human.{key}") for value in values)
    owner = human.get("owner_user")
    if owner is not None:
        string(owner, "always_human.owner_user")
    human = AlwaysHuman(user_ids("users"), user_ids("mention_users"), owner)
    classifier = mapping(data.get("classifier", {}), "classifier")
    kind = classifier.get("kind", "none")
    if kind not in ("none", "anthropic"):
        raise ConfigError("classifier.kind is unsupported")
    enabled = boolean(classifier.get("enabled", False), "classifier.enabled")
    model = classifier.get("model", "")
    if not isinstance(model, str):
        raise ConfigError("classifier.model must be a string")
    if enabled:
        if kind == "none":
            raise ConfigError("classifier.enabled requires anthropic kind")
        string(model, "classifier.model")
    descriptions = {}
    for lane, description in mapping(classifier.get("lane_descriptions", {}),
                                     "classifier.lane_descriptions").items():
        descriptions[lane_ref(lane, "classifier lane")] = string(description, "lane description")
    classifier = Classifier(
        kind, enabled, model,
        string(classifier.get("api_key_env", "ANTHROPIC_API_KEY"), "classifier.api_key_env"),
        string(classifier.get("taxonomy_version", "v1"), "classifier.taxonomy_version"),
        descriptions,
    )
    pattern = string(data.get("explicit_tag_pattern", r"\[(?P<lane>[a-z0-9-]+)\]"),
                     "explicit_tag_pattern")
    try:
        compiled = re.compile(pattern)
    except re.error:
        raise ConfigError("explicit_tag_pattern must be a valid regex") from None
    if "lane" not in compiled.groupindex:
        raise ConfigError("explicit_tag_pattern requires a named lane group")
    service = mapping(data.get("service", {}), "service")
    level = string(service.get("log_level", "INFO"), "service.log_level").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError("service.log_level is unsupported")
    service = Service(
        state_dir=string(service.get("state_dir", "state"), "service.state_dir"),
        heartbeat_seconds=positive(service.get("heartbeat_seconds", 30),
                                   "service.heartbeat_seconds"),
        log_dir=string(service.get("log_dir", "logs"), "service.log_dir"),
        log_level=level,
        once=boolean(service.get("once", False), "service.once"),
        followup_window_seconds=positive(service.get("followup_window_seconds", 600),
                                        "service.followup_window_seconds"),
    )
    return Config(tuple(lanes), defaults("channel_defaults"), defaults("dm_defaults"),
                  tuple(rules), tags, excluded, timers, wake, tuple(adapters), tuple(sources),
                  ledger=ledger, always_human=human, classifier=classifier,
                  explicit_tag_pattern=pattern, inbox=inbox, service=service)


def load_config(path: str | Path) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigError("Cannot read config: provide a readable UTF-8 YAML file") from None
    return parse_config(raw)
