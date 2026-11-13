"""Load YAML only; never read credentials, expand environment, or start sources."""
from pathlib import Path
import re

import yaml

from .schema import (
    AlwaysHuman, BotMessages, Classifier, Config, Console, Decisions, HumanQueue, Inbox, Lane,
    LedgerConfig, NotifyAdapter, OwnerSignals, PersonChannelRule, ScopeLearning, Service,
    SourceConfig, Timers, WakePolicy,
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
        if kind not in ("file_inbox", "session_message", "assistant_dm", "file_drop", "dispatch_card"):
            raise ConfigError("notify_adapter.kind is unsupported")
        path = row.get("outbox_dir", row.get("path", inbox.root if kind == "file_inbox" else None))
        if kind == "dispatch_card":
            path = row.get("dir", row.get("path"))
        if kind != "session_message" or path is not None:
            string(path, "notify_adapter.path")
        command = row.get("command_template", [])
        if not isinstance(command, list) or any(not isinstance(v, str) or not v for v in command):
            raise ConfigError("notify_adapter.command_template must be a string list")
        template = mapping(row.get("template", {"lane": "{lane}", "ids": "{ids}", "count": "{count}"}), "notify_adapter.template")
        purpose = row.get("purpose", row.get("for", "lane"))
        if purpose not in ("lane", "human_queue"):
            raise ConfigError("notify_adapter.purpose must be lane or human_queue")
        only_immediate, domain = card_options(row, kind, purpose)
        adapters.append(NotifyAdapter(kind, path, tuple(command),
                                      boolean(row.get("dry_run", False), "notify_adapter.dry_run"),
                                      positive(row.get("timeout", 60), "notify_adapter.timeout"),
                                      template, purpose, only_immediate, domain))
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
                                    ledger.store_body,
                                    boolean(row.get("discover_channels", False), "source.discover_channels"),
                                    excluded,
                                    replies=boolean(row.get("replies", True), "source.replies"),
                                    replies_lookback_days=positive(row.get("replies_lookback_days", row.get("lookback_days", 7)), "source.replies_lookback_days"),
                                    replies_overlap_seconds=positive(row.get("replies_overlap_seconds", 120), "source.replies_overlap_seconds"),
                                    bound_threads=boolean(row.get("bound_threads", True), "source.bound_threads"),
                                    bound_threads_max_age_days=positive(row.get("bound_threads_max_age_days", 14),
                                                                        "source.bound_threads_max_age_days"),
                                    permanent_error_ttl_seconds=positive(
                                        row.get("permanent_error_ttl_seconds", 3600),
                                        "source.permanent_error_ttl_seconds")))
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
        gap_alert_seconds=positive(service.get("gap_alert_seconds", 300), "service.gap_alert_seconds"),
        state_dir=string(service.get("state_dir", "state"), "service.state_dir"),
        heartbeat_seconds=positive(service.get("heartbeat_seconds", 30),
                                   "service.heartbeat_seconds"),
        log_dir=string(service.get("log_dir", "logs"), "service.log_dir"),
        log_level=level,
        once=boolean(service.get("once", False), "service.once"),
        followup_window_seconds=positive(service.get("followup_window_seconds", 600),
                                        "service.followup_window_seconds"),
    )
    queue = mapping(data.get("human_queue", {}), "human_queue")
    mode = queue.get("mode", "digest")
    if mode not in ("digest", "immediate"):
        raise ConfigError("human_queue.mode must be digest or immediate")
    flags = queue.get("immediate_flags", ["always_human", "owner_mention"])
    if not isinstance(flags, list) or any(flag not in ("always_human", "owner_mention") for flag in flags):
        raise ConfigError("human_queue.immediate_flags must contain supported flags")
    queue = HumanQueue(mode, positive(queue.get("digest_seconds", 1800),
                                     "human_queue.digest_seconds"), tuple(flags))
    bots = mapping(data.get("bot_messages", {}), "bot_messages")
    policy = bots.get("policy", "route_or_exclude")
    if policy not in ("route_or_exclude", "exclude_all", "route_all"):
        raise ConfigError("bot_messages.policy is unsupported")
    known = bots.get("known_bot_users", [])
    if not isinstance(known, list):
        raise ConfigError("bot_messages.known_bot_users must be a list")
    bots = BotMessages(policy, tuple(string(value, "bot_messages.known_bot_users") for value in known))
    signals = mapping(data.get("owner_signals", {}), "owner_signals")
    posts = signals.get("posts", "record")
    if posts not in ("record", "route"):
        raise ConfigError("owner_signals.posts must be record or route")
    def reactions(key, default):
        values = signals.get(key, default)
        if not isinstance(values, list) or any(not isinstance(value, str) or
                not re.fullmatch(r"[A-Za-z0-9_+\-]{1,100}", value) for value in values):
            raise ConfigError("owner_signals reactions must be emoji name lists")
        return tuple(values)
    signals = OwnerSignals(posts, reactions("reactions_done", ["white_check_mark", "heavy_check_mark"]),
                           reactions("reactions_ack", ["eyes"]))
    sessions_dir = string(data.get("sessions_dir", Config.sessions_dir), "sessions_dir")
    people = {string(key, "people key"): string(value, "people name")
              for key, value in mapping(data.get("people", {}), "people").items()}
    zone = timezone_name(data.get("display_timezone", Config.display_timezone))
    console = console_section(data.get("console", {}), ids)
    decisions = decisions_section(data.get("decisions", {}))
    learning = scope_learning_section(data.get("scope_learning", {}))
    return Config(tuple(lanes), defaults("channel_defaults"), defaults("dm_defaults"),
                  tuple(rules), tags, excluded, timers, wake, tuple(adapters), tuple(sources),
                  ledger=ledger, always_human=human, classifier=classifier,
                  explicit_tag_pattern=pattern, inbox=inbox, service=service,
                  human_queue=queue, bot_messages=bots, owner_signals=signals,
                  sessions_dir=sessions_dir, people=people, display_timezone=zone,
                  console=console, decisions=decisions, scope_learning=learning)


def scope_learning_section(value):
    """Optional `scope_learning` map (P13). `min_evidence` is a positive integer, default 2."""
    data = mapping({} if value is None else value, "scope_learning")
    return ScopeLearning(min_evidence=positive(data.get("min_evidence", ScopeLearning.min_evidence),
                                               "scope_learning.min_evidence"))


def card_options(row, kind, purpose):
    """P12 dispatch_card keys: purpose human_queue, `only_immediate`, `workspace_domain`."""
    if kind != "dispatch_card":
        return True, None
    if purpose != "human_queue":
        raise ConfigError("notify_adapter dispatch_card requires purpose human_queue")
    only_immediate = boolean(row.get("only_immediate", True), "notify_adapter.only_immediate")
    domain = row.get("workspace_domain")
    if domain is not None and (not isinstance(domain, str)
                               or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", domain)):
        raise ConfigError("notify_adapter.workspace_domain must be a Slack workspace subdomain")
    return only_immediate, domain


def decisions_section(value):
    """Optional `decisions` map (P12). Every key is optional."""
    data = mapping({} if value is None else value, "decisions")
    directory = data.get("dir")
    if directory is not None:
        string(directory, "decisions.dir")
    key_file = data.get("hmac_key_file")
    if key_file is not None:
        string(key_file, "decisions.hmac_key_file")
    return Decisions(dir=directory,
                     poll_seconds=positive(data.get("poll_seconds", Decisions.poll_seconds),
                                           "decisions.poll_seconds"),
                     hmac_key_file=key_file)


UI_LANGUAGES = ("en", "ko")


def console_section(value, lane_ids):
    """Optional `console` map (P10). Every key is optional; omitted keys keep defaults."""
    data = mapping({} if value is None else value, "console")
    host = string(data.get("host", Console.host), "console.host")
    port = data.get("port", Console.port)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("console.port must be an integer from 1 to 65535")
    base_url = data.get("base_url")
    if base_url is not None:
        string(base_url, "console.base_url")
        if not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", base_url):
            raise ConfigError("console.base_url must be an http or https URL without query or fragment")
        base_url = base_url.rstrip("/")
    page_size = positive(data.get("page_size", Console.page_size), "console.page_size")
    if page_size > 500:
        raise ConfigError("console.page_size must not exceed 500")
    hint = boolean(data.get("lane_hint_people", Console.lane_hint_people), "console.lane_hint_people")
    direct = Console.direct_lane
    if "direct_lane" in data:
        # An explicit direct lane must be configured; the default is used only when it is.
        direct = string(data["direct_lane"], "console.direct_lane")
        if direct not in lane_ids:
            raise ConfigError("console.direct_lane must reference a configured lane")
    channels = {string(key, "console.channels key"): string(name, "console.channels name")
                for key, name in mapping(data.get("channels", {}) or {}, "console.channels").items()}
    language = string(data.get("ui_language", Console.ui_language), "console.ui_language")
    if language not in UI_LANGUAGES:
        raise ConfigError("console.ui_language must be one of: " + ", ".join(UI_LANGUAGES))
    return Console(host=host, port=port, base_url=base_url, page_size=page_size,
                   lane_hint_people=hint, direct_lane=direct, channels=channels,
                   ui_language=language)


def timezone_name(value):
    """Validate an IANA zone name. UTC is always accepted.

    When the host has no time zone database (for example Windows without the
    tzdata package) other names cannot be checked here; the inbox CLI then falls
    back to UTC with a warning.
    """
    string(value, "display_timezone")
    if value == "UTC":
        return value
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(value)
        return value
    except ZoneInfoNotFoundError:
        pass
    except (ValueError, OSError):
        raise ConfigError("display_timezone must be an IANA time zone name") from None
    try:
        ZoneInfo("Etc/UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return value  # no database on this host: the name cannot be verified here
    raise ConfigError("display_timezone must be an IANA time zone name")


def load_config(path: str | Path) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigError("Cannot read config: provide a readable UTF-8 YAML file") from None
    return parse_config(raw)
