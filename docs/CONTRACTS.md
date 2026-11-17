# slack-dispatch phase 1 contracts (v0.1 target)

These contracts let three packets (ingest+ledger, router+queue, inbox+notify) be built in parallel against the v0.0.1 scaffold. Packets implement or consume exactly these shapes. Additive changes only; record any deviation in `CONTRACT_NOTES.md` inside the packet.

## 1. Normalized event (Source -> service -> ledger)

`Event = dict[str, Any]` with keys:

| key | type | notes |
|---|---|---|
| source | str | "slack" |
| event_id | str | Slack event_id, globally unique; dedup key |
| event_type | str | message, reaction_added, reaction_removed, file_shared, channel_created, member_joined_channel |
| subtype | str or None | e.g. message_changed, message_deleted, thread_broadcast |
| channel | str or None | conversation id |
| channel_type | str or None | channel, group, im, mpim |
| user | str or None | author or actor id |
| is_bot | bool | Additive service Event field, set once by the pipeline (P16): missing user, subtype bot_message, configured bot user, or a bot per the user directory; bot_id alone is not a bot |
| ts | str or None | message ts; for message_changed the edited message ts; for reactions the reacted item ts |
| thread_ts | str or None | parent ts when the message is a thread reply |
| is_thread_reply | bool | thread_ts present and thread_ts != ts |
| has_files | bool | |
| reaction | str or None | reaction name for reaction events |
| received_at | str | ISO-8601 local time with offset |
| event_time | int or None | Slack event_time |
| retry_attempt | int or None | Socket Mode retry metadata |
| text | str or None | message body. Present only when `ledger.store_body` is true; never printed or logged by any component |

## 2. Message id

`message_id = f"{source}:{channel}:{ts}"`. Stable and derivable from any event that references a message. A `message_changed` event updates the existing row (sets `edited_at`, `followup=1`) and never creates a new row.

## 3. LedgerStore API (package `slack_dispatch.ledger.store`)

Constructor `LedgerStore(path)`; `open()` enables WAL and foreign keys; `migrate()` applies `schema.sql` through a `schema_version` table; `close()`. All mutations run through this single object using `BEGIN IMMEDIATE`.

```
record_event(event: Event) -> RecordResult        # RecordResult(message_id: str | None, created: bool, duplicate: bool, kind: str, previous_body: str | None = None)
get_message(message_id: str) -> Message | None    # Message = dict with the messages row (body omitted unless include_body=True)
list_messages(*, state: str | None = None, lane: str | None = None, since: str | None = None, limit: int = 100) -> list[Message]
set_route(message_id: str, lane: str, reason: str, *, state: str = "assigned") -> None
set_state(message_id: str, state: str, *, note: str | None = None) -> None   # states: new, assigned, acked, done, human_pending, excluded
set_suggestion(message_id: str, lane: str, reason: str, classifier_version: str) -> None
mark_followup(message_id: str) -> None
bind_thread(source: str, channel: str, thread_ts: str, lane: str) -> None
lookup_binding(source: str, channel: str, thread_ts: str) -> str | None
upsert_lane(lane_id: str, *, session_pointer: str | None = None, paused: bool | None = None) -> None
get_lane(lane_id: str) -> dict | None
list_lanes() -> list[dict]
get_cursor(source: str, channel: str) -> str | None
set_cursor(source: str, channel: str, cursor: str) -> None
list_reactions(message_id: str) -> list[dict]
```

`record_event` behaviour: message events insert (state `new`) or update; reaction events insert into `reactions` and return the referenced message id (created=False); file_shared with a message ts marks `has_files=1` on that message; channel_created and member_joined_channel are stored in `events_seen` only (kind="channel_meta"). Any event whose event_id was already seen returns duplicate=True with no change.

Schema additions owned by the ingest+ledger packet (additive to the scaffold `schema.sql`): `schema_version(version INTEGER)`, `events_seen(event_id PK, received_at, kind, message_id)`, `reactions(message_id, user, reaction, added_at, removed_at)`, columns `messages.event_id`, `messages.channel_type`, `messages.has_files`, `messages.edited_at`, `messages.note`.

Other packets must not edit `schema.sql` or `store.py`; their tests use an in-memory fake implementing the method names above.

## 4. Router contract (package `slack_dispatch.router`)

```
Decision(lane: str | None, reason: str, rule: str | None, attention: str | None = None)       # rule in thread_inherit, explicit_tag, channel_default, person_channel, always_human, excluded, none
route(message: Message, config: Config, lookup_binding: Callable[[str, str, str], str | None]) -> Decision
```
Order: excluded channel -> bot policy -> collect always-human attention ->
mention-only gate (P26) -> ThreadInherit -> ExplicitTag -> ChannelDefault ->
PersonChannel -> human pending.
Attention is independent of lane selection. An unmatched protected item uses
rule=always_human; an unmatched ordinary item uses rule=none. Only rule=none may
invoke a suggester. See ROUTING.md for the full precedence table.

## 5. Inbox record (per-lane JSONL, `inbox/<lane>.jsonl`)

`{"id": message_id, "delivered_at": iso, "channel": str, "ts": str, "thread_ts": str | None, "followup": bool, "kind": "new" | "followup" | "reroute" | "backfill"}`. No body. Writers hold `inbox/<lane>.lock` while appending. Kind `followup` means edited (message_changed, ledger `edited_at`); a reply within the followup window keeps `new` or `backfill` with `followup: true` (P9).

## 6. Notifier contract (package `slack_dispatch.notify`)

`Notifier.notify(lane: str, message_ids: list[str]) -> None`. Payloads carry ids and counts only. Adapters: `FileInboxNotifier(root)`, `SessionMessageNotifier(command_template, target_resolver, dry_run)`, `FileDropNotifier(outbox_dir, template)`. Wake policy (`notify/wake.py`) decides when to call notifiers: immediate by default, 90 s debounce per (lane, author), per-lane storm guard (30 per hour -> 600 s batching), global switch (config flag or presence of file `<state>/wake.off`), immediate triggers for configured user ids and owner mentions.

## 7. Config additions (owned by each packet, additive to `config/schema.py`)

- ingest+ledger: `ledger: {path, store_body: bool=false}`, `sources[].kind == "slack_socket"` with `env_file`, `app_token_key`, `user_token_key`; `sources[].kind == "slack_reconcile"` with `interval_seconds`, `lookback_days`.
- router+queue: `always_human: {users: [..], mention_users: [..]}`, `classifier: {kind: "none" | "anthropic", model, api_key_env, taxonomy_version, enabled: false}`, `explicit_tag_pattern` default `\[(?P<lane>[a-z0-9-]+)\]`.
- inbox+notify: `inbox: {root}`, `notify_adapters[]` kinds `file_inbox`, `session_message`, `file_drop`; `wake_policy.immediate_users`, `wake_policy.owner_user`.

Loader changes must keep the existing sample config loading and add the new keys to `examples/instance-sample/config.yaml` with dummy values. Each packet edits only its own keys and adds its own tests for them.

## Shadow-day additive contracts

- Config adds human_queue (mode=digest, digest_seconds=1800,
  immediate_flags=[always_human, owner_mention]), bot_messages
  (policy=route_or_exclude, known_bot_users=[]), and
  sources[].discover_channels=false. mode=immediate preserves per-item delivery.
- SourceConfig carries excluded_channels from the global configuration for
  discovery even when constructed through the standalone source factory.
- The service source wrappers preserve raw bot evidence before legacy
  normalization discards bot_id. Every Event they deliver has boolean is_bot.
  Pipeline callers may supply is_bot or bot_id; missing users and configured bot
  users are also recognized. P7A also preserves bot_id in shared normalization.
  Superseded by P16: sources no longer set is_bot and bot_id alone is not a bot.
- Ledger v1 migration idempotently adds messages.attention (nullable TEXT),
  messages.is_bot, human_notices, and timer_events. Existing v1 data and the
  schema_version value remain intact. set_attention(id, value) accepts None,
  always_human, or owner_mention. Old rows default to is_bot=0 until processed.
- human_notices holds message ids, trigger kinds, adapter positions, queue times,
  immediate flags, and delivery receipts. It never holds source text. Timer
  receipts are committed only after successful notification, including ids not
  displayed in the 20-id digest preview. Adapter positions must remain stable.
- The base notify(lane, ids) protocol is unchanged. Built-in session and file-drop
  human adapters receive generated summaries through copied templates. Human
  file-inbox adapters write atomic summary notices under
  <inbox-root>/human_queue-notices/, preserving lane JSONL record contracts.
  Custom adapters implementing only notify receive the capped ids list.
- Global wake controls apply to lane wakes only. Human queue delivery retains
  adapter dry-run. Since P9 it no longer uses lane session availability or pause:
  the operator target is the adapter's template.target (default `operator`).

## P7A coverage

Event adds optional origin (`socket`, `reconcile`, `hydrate`), prehandled_by
(`owner_reply:<ts>`), normalized bot_id and reaction summaries (`name`, `users`).
Body text is flattened from supported blocks when requested. Raw blocks and
file contents are not copied into the Event. File projections keep id and, only
when source body access is enabled, name/title. File-only text uses file names or
titles. Hydrated context roots are done with route_reason=context:root and
note=context. They do not count as events routed or delivered.

Ledger schema version 2 idempotently upgrades version 1, preserving existing
rows and adding nullable messages.origin (old rows stay null) and ingest_gaps:
`id INTEGER PRIMARY KEY, source TEXT, detected_at TEXT, last_event_at TEXT,
resumed_at TEXT, catchup_at TEXT, note TEXT`. Unknown future versions fail.
The new cursor source is slack_replies, keyed by channel independently of slack.

Inbox kind additionally accepts `backfill`. files.record derives it from
origin=reconcile only when no explicit kind and no edit marker (edited_at) are
present; since P9 a followup marker alone keeps `backfill` or `new`.
FileInboxNotifier retains the origin through its existing row lookup. The read
signature and output keys are unchanged; consumers can treat kind as opaque.
Pipeline HandleResult adds `backfill: bool = False`, true when a reconcile
message reached a file inbox without a wake (P23: false for a catch-up wake, see below).

Owner posts under record policy have state=done, route_reason=owner_post and
note=owner_post. Prehandled items have state=done, route_reason=prehandled and
note=prehandled:reaction:<emoji> or prehandled:owner_reply. Neither path changes
thread bindings, invokes a classifier or sends notifications.

## P9 shadow follow-ups

All additive. Config: optional top-level `people` (map of user id to display name,
non-empty strings) and `display_timezone` (IANA name, default `UTC`; checked when the
host has a time zone database); optional `sources[].permanent_error_ttl_seconds`
(positive integer, default 3600). `NotifyAdapter.template.target` (string) names the
human_queue session target. No existing key, default or required section changes.

- `notify.human_queue_target(adapter)` returns template.target or `operator`.
  `can_notify` returns True for purpose human_queue without lane lookup, session
  resolution, pause check or wake switch. `can_notify` gains an optional
  `message_ids` argument.
- `notify.wake_switch_path(config)` is the one wake.off path for the builder, the
  gate and the service engine (wake_policy.state_dir when not `state`, else
  service.state_dir).
- `SessionResolver.resolve_target` gains optional `message_ids`;
  `skipped_no_session` is a read-only count of distinct (lane, message id) pairs.
  Not-live and ambiguous lines are logged per lane on transitions with a 30 minute
  reminder.
- `SlackReconcileSource.skipped_parents` becomes a dict `(channel, parent) ->
  skipped_at` (pass start time). New `open_client()` and `fetch_message(channel, ts,
  thread_ts=None)` read helpers use the existing allowlist and 429 handling.
- `LedgerStore.set_body(message_id, body) -> bool` fills a null body when
  store_body is enabled; deleted rows and rows with a body are unchanged.
- Inbox records: kind `followup` only for edited messages (see section 5). CLI rows
  add `when`; JSON rows add `ts` and `author_id`; thread JSON roots and items add
  `when` and `author_id`.
- Human notices end with `Lane ids for assign: <ids>`.
- `python -m slack_dispatch.service --config PATH --rehydrate-bodies DAYS` is a
  one-pass exclusive mode (see SERVICE.md).

## P12 cutover and cards

All additive.

- Ledger: column `messages.deleted_at` (TEXT, set by `message_deleted`; an open row
  also becomes `done` with `done_at`, the note stays `message_deleted`) and table
  `wake_requests(id INTEGER PRIMARY KEY, lane TEXT, message_id TEXT, requested_at TEXT,
  source TEXT, consumed_at TEXT)`, both created by `migrate()`; the schema version
  stays 2.
- `LedgerStore` adds `lane_messages(lane)`, `thread_delivery(source, channel,
  thread_ts, lane)` (None without the service table), `request_wake(lane, message_id,
  source)`, `pending_wake_requests(limit=500)` and `consume_wake_requests(ids, at=None)`.
- `console.actions.assign` and `reroute` queue one wake request each (source = the
  reason without `human:`, `cli` for the CLI); `reroute` gains `source="console"`.
  `inbox_refusal(message)` returns the refusal line for context rows and owner posts;
  `assign` and `reroute` raise `ActionError("not_inbox_item")` for them. Stores without
  `request_wake` (test fakes) skip the request.
- Inbox CLI: `cutover` command; `--config` accepted globally; route reason
  `human:card` for card decisions; inbox kind `backfill` is also written by cutover.
- Config: `notify_adapters[].kind == "dispatch_card"` (purpose `human_queue` required,
  YAML `dir` stored in `NotifyAdapter.path`, `only_immediate: bool = True`,
  `workspace_domain: str | None`), and an optional `decisions` section
  (`dir: str | None`, `poll_seconds: int = 5`, `hmac_key_file: str | None`).

## P13 scope learning

All additive; the schema version stays 2.

- Ledger tables created by `migrate()`: `scope_claims(id, lane, kind channel|dm|person|thread,
  key, note, claimed_at, claimed_by, status claimed|confirmed, confirmed_at, confirmed_by,
  removed_at)`; `scope_proposals(kind channel|dm|person, key, lane, evidence_count,
  first_seen, last_seen, status open|suggested|confirmed|rejected|deferred|superseded,
  conflict, last_evidence, imported, decided_at)` keyed by (kind, key, lane); and
  `sends(channel, ts, thread_ts, lane, kind, recorded_at)` keyed by (channel, ts).
- `LedgerStore` adds `add_scope_claim`, `remove_scope_claim`, `list_scope_claims`,
  `scope_claim_lanes`, `lookup_scope_claim(kind, key)`, `confirmed_scope_claims`,
  `confirm_scope_claim`, `get_scope_proposal`, `list_scope_proposals`,
  `record_scope_evidence(kind, key, lane, source, *, min_evidence=2)`,
  `import_scope_proposal`, `decide_scope_proposal(kind, key, lane, decision, *, by,
  min_evidence)` (raises KeyError or `ProposalStateError`), `record_send` and `list_sends`.
- Router: rule `ScopeClaim` after ThreadInherit (decision rule `scope_claim`, reasons
  `scope_claim` and `scope:auto`); `Route` gains an optional `rule`; `route()` gains an
  optional `lookup_scope`. `router.apply.record_scope_proposal(store, message, lane,
  source, *, min_evidence=None)` is the shared learning hook.
- `console.actions.assign`, `reroute` and `apply` gain `min_evidence`; `assign` and
  `reroute` bind the message thread to the lane.
- Inbox CLI: `scope add|list|remove|import` and `sent`; `cutover` skips unconsumed backfill.
- Config: optional `scope_learning: {min_evidence: 2}` (positive integer).
- Console: page `/proposals`, `GET /api/proposals`, `POST /api/proposals/actions`,
  `GET /api/proposals/export`.

### Card request `dispatch_card/1` (dispatcher writes, Jarvis reads)

`<dir>/cards/<message_id_safe>.json`, `message_id_safe = message_id` with `:` and `.`
replaced by `_`, written as `<dir>/cards/.tmp/<name>.part` and renamed. A card is
written only for a message still in the human queue (state `human_pending`): an
attention item the router already assigned to a lane gets its immediate FYI notice on
the other human-queue adapters (session_message, file_drop and so on) but no card, and
a queued card whose message has left `human_pending` before delivery is dropped.


```json
{"schema": "dispatch_card/1", "message_id": "slack:C0000000002:1000.000001",
 "title": "[slack-dispatch] owner mention dummy-general", "body": "...",
 "lanes": ["support", "content"], "suggested_lane": "support",
 "attention": "owner_mention", "permalink": "https://example.slack.com/archives/C0000000002/p1000000001",
 "created_at": "2026-01-01T09:00:00.000000+09:00"}
```

- `title`: `[slack-dispatch] <attention with spaces, or "human queue"> <console channel
  name or channel id>`, at most 120 characters.
- `body`: the first 1500 characters of the stored body, else the ids-only notice
  summary. dispatch_card/1 has no `console_url` key and Jarvis rejects unknown keys,
  so with `console.base_url` the last body line is `Open: <base_url>/#msg=<id>` and the
  stored body is cut to keep the total at 1500.
- `lanes`: 1 to 5 configured lane ids without `human_queue` and without the direct lane
  (`console.direct_lane`, which the fixed `direct` button already covers; when no
  configured lane is the direct lane nothing else is left out); the classifier
  suggestion first, then the channel or DM default of the channel, then the thread
  binding, then the rest alphabetically. Ids Jarvis does not accept (`direct`,
  `exclude`, or not matching `[a-z0-9][a-z0-9_-]{0,29}`) are left out; with none left no
  card is written.
- `suggested_lane`: the classifier suggestion when it is in `lanes`, else null.
  `attention`: `owner_mention`, `always_human` or null. `permalink`:
  `https://<workspace_domain>.slack.com/archives/<channel>/p<ts without the dot>` or
  null. `created_at`: local ISO time with offset.

### Decision `dispatch_decision/1` (Jarvis writes, dispatcher reads)

`<dir>/decisions/<message_id_safe>.json` with exactly `schema`, `message_id`, `lane`
(a lane id, `direct` or `exclude`), `decided_by`, `decided_at`, `card_ts` and `hmac` =
hex HMAC-SHA256 with the key bytes of `DISPATCH_HMAC_KEY=<64 hex>` over
`json.dumps(<all keys but hmac>, sort_keys=True, ensure_ascii=False,
separators=(",", ":"))` in UTF-8. Processed files move to `decisions/done/`, invalid
ones to `decisions/rejected/`; rejection codes are `json`, `keys`, `schema`,
`file_name`, `hmac`, `decided_by`, `already_decided`, `unknown_message`,
`not_inbox_item`, `closed`, `direct_lane`, `lane` and `lane_not_found`.

## P15 membership filter

All additive; the schema version stays 2.

- Ledger tables created by `migrate()`: `channel_membership(channel PK, kind, name,
  partner_user, is_member, seen_at)` and `channel_exclusions(id, channel, excluded_at, by,
  removed_at)`.
- `LedgerStore` adds `replace_membership(rows, seen_at)`, `list_membership()`,
  `add_channel_exclusion(channel, by)`, `remove_channel_exclusion(channel)`,
  `list_channel_exclusions(*, include_removed=False)`, `channel_exclusion_ids()` and
  `close_channel_items(channel, note, *, source="slack")`.
- `slack_dispatch.ingest.membership`: `MembershipCache(client, store, refresh_seconds=3600,
  *, policy, clock, configured_channels)` with `load`, `refresh`, `maybe_refresh`,
  `is_member`, `excludes`, `member_channels`, `snapshot`; `member_status`, `NOT_MEMBER`
  (`excluded:not_member`). `users.conversations` joins the reconcile read allowlist.
- `SlackReconcileSource.membership` (optional cache) and `scan_channels(channels)`.
  `Pipeline.membership` (optional cache), `Pipeline.routing_config()` and
  `pipeline.routing_config(config, store)`; `service.build_membership(config, store,
  sources, clock)`. `route()` itself is unchanged.
- Heartbeat counters `member_channels` and `excluded_not_member` when the cache exists.
- Config: optional `membership: {enabled: true, refresh_seconds: 3600, policy: exclude|off}`.
- Console: queue action `exclude_channel` (result `closed`, `channel`), queue item
  `not_member`, `GET /api/proposals` adds `channel_exclusions`,
  `POST /api/channel-exclusions/undo`, `GET /api/channel-exclusions/export`.

## P16 bot flag and user directory

All additive; the schema version stays 2.

- `slack_dispatch.ingest.user_directory`: `is_bot_message(event, config, directory=None)
  -> bool` (the one rule, see ROUTING.md), `carries_app_evidence(event)`,
  `UserDirectory(client, store, ttl_seconds=86400, known_bot_users=(), *, clock)` with
  `is_bot(user)` and `snapshot()`. `users.info` joins the reconcile `READ_METHODS`.
- Event `is_bot` is decided once by `Pipeline.is_bot(event)`; socket and reconcile
  sources no longer set it. A caller-supplied `is_bot: True` is kept; `bot_id` alone is
  human. `Pipeline.directory` (optional) is set by `service.build_user_directory(config,
  store, sources, clock)`.
- Router: `route()` signature unchanged; it uses `message["is_bot"]` when present, else
  `is_bot_message(message, config)`. New helpers `message_is_bot(message, config)` and
  `attention_for(message, config)`.
- Ledger table created by `migrate()`: `user_directory(user TEXT PRIMARY KEY, is_bot
  INTEGER, is_app_user INTEGER, deleted INTEGER, name TEXT, seen_at TEXT)`.
  `LedgerStore` adds `get_user_directory(user)`, `put_user_directory(row, seen_at)`,
  `list_user_directory()`, `bot_flagged_messages(*, since=None)` and
  `set_is_bot(message_id, value)`. `record_event` stores the event's `is_bot` when
  present, else `is_bot_message(event, None)`.
- `pipeline.queue_human_notice(config, store, mid, kind, *, immediate, adapter, now)`
  queues human notice rows without delivery (used by `HumanQueueDelivery.offer` and the
  repair command).
- Inbox CLI: `repair bot-flag [--apply] [--since-days N] [--assume-human] [--json]`.
- Config: optional `user_directory: {enabled: true, ttl_seconds: 86400,
  failure_cooldown_seconds: 300}` (positive integers). `UserDirectory` gains the
  keyword `failure_cooldown_seconds`; a failed refresh keeps an expired cached value.
- Heartbeat counters `directory_lookups`, `directory_failures`, `app_posts_human`,
  `bot_posts` when the directory exists.

## P17 owner resolver and registry sync

All additive; the schema version stays 2 and no table changes.

- `slack_dispatch.resolver.registry`: `RegistryIndex.load(path, *, closed_statuses)`
  (cached per mtime and size, failure keeps the previous index), `RegistryIndex.parse(data,
  ...)`, `rows_for(channel, root)`, `channel_rows(channel)`, `match(channel, ts,
  thread_ts)`, `effective(rows)`, `sweep_for(conversation)`, `rules_for(conversation)`;
  `RegistryRow` (`session_ids`, `closed`, `last_ts`, `summary()`), `lane_for_session(config,
  session)`, `registry_info(row, message_ts)` (badge data with `registry_closed` and the
  done note `registry:<status>:<registry id>`).
- `slack_dispatch.resolver.owner`: `resolve(store, config, registry, channel, ts=None,
  thread_ts=None, *, harvest=None, now=None) -> Resolution` with `candidates`
  (`Candidate(tier, lane, source, evidence, session)`), `best`, `confidence`, `reason()`,
  `registry_row`, `as_dict()`; `OwnerResolver(config, *, clock)` with `registry()`,
  `resolve(store, channel, ts, thread_ts)`, `registry_for(message)`; `HarvestIndex.load(path)`;
  `VERSION = "owner-resolver/1"`. Tiers: `certain`, `declared`, `recorded`, `observed`.
  Sources: `binding`, `scope_claim`, `channel_default`, `dm_default`, `registry`,
  `registry_channel`, `sweep_scope`, `routing_rule`, `activity`, `harvest`.
- `LedgerStore` adds reads only: `channel_lane_activity(channel, *, source="slack")`,
  `pending_in_thread(source, channel, root)` and `open_messages(*, lane=None)`.
- Pipeline: `Pipeline.owner_resolver` (None unless enabled), `OwnerFirstSuggester`
  (resolver first, classifier for rows without a lane), `build_owner_resolver(config)`;
  an owner thread reply closes earlier `human_pending` items of the thread (note
  `owner_reply`). `route()` and `apply_decision()` are unchanged.
- Console: `create_app(..., owner=None)`; `GET /api/config` adds `owner_resolver`;
  `GET /api/queue` adds `registry_closed` and item `registry`; lane inbox groups and items
  add `registry` (items also `channel`); new `GET /api/owner`. Staged registry decisions
  use the existing `done` commit with the registry note.
- Inbox CLI: `owner CHANNEL [--ts TS] [--json]`, `owner --pending [--apply] [--json]`,
  `registry check [--lane LANE] [--json]`.
- Config: optional `owner_resolver: {enabled: false, registry_path: null, harvest_path:
  null, refresh_seconds: 300, closed_statuses: [closed, answered, answered_action_ours,
  sent_closed_no_action_ours, delivered], close_queue_on_owner_reply: true,
  activity_days: 60}`.
- Heartbeat counter `owner_reply_closed` when the resolver is enabled.

## P18 delivery log

All additive; the schema version stays 2.

- Ledger table created by `migrate()`: `delivery_log(id INTEGER PRIMARY KEY, created_at TEXT,
  purpose TEXT, lane TEXT, kind TEXT, adapter TEXT, target_session_id TEXT,
  target_session_name TEXT, item_count INTEGER, item_ids TEXT, status TEXT, detail TEXT,
  cost_usd REAL, duration_ms INTEGER, relay_session_id TEXT, last_seen_at TEXT,
  repeat_count INTEGER)` with an index on `created_at`; `migrate()` adds the last two
  columns to an earlier table. `created_at` is UTC ISO with microseconds (the relay start
  for relay attempts); `item_ids` is a JSON list. `add_delivery` folds a skipped row equal
  to the lane's previous row into it (`last_seen_at`, `repeat_count`).
- `LedgerStore` adds `add_delivery(row)`, `list_deliveries(*, since, lane, purpose, status,
  limit, offset)` (status `skipped` matches every `skipped_*`), `count_deliveries(...)`,
  `prune_deliveries(before)` and `delivery_receipts(rows, now=None)`; module helpers
  `utc_stamp(epoch)` and `delivery_item_ids(value)`.
- `slack_dispatch.notify.base`: `DeliveryRecorder(store, *, purpose, adapter, clock,
  delivered_patterns, not_delivered_patterns)` with `attempt(kind, ids)` and
  `record(lane, ids, status, **fields, at=None)` (`at` = attempt start, epoch seconds),
  `now()`, `error_detail(error)`, `DELIVERY_STATUSES`.
- Adapters that log their own attempts set `logs_deliveries = True` and read
  `delivery_recorder` (`SessionMessageNotifier`, `DispatchCardNotifier`). The ids-only
  `notify(lane, ids)` protocol is unchanged.
- `slack_dispatch.notify.session_message`: `relay_result(stdout)`, `classify_relay(text,
  delivered, not_delivered)`, `relay_fields(stdout, command, delivered, not_delivered)`.
  `SessionResolver` adds `resolved` and `resolved_session(lane, target)`.
- `slack_dispatch.pipeline`: `skip_reason`, `lane_kind`, `human_kind`, `build_recorders`,
  `logged_call`; `Pipeline.recorders`, `Pipeline.prune_deliveries(now=None)`;
  `HumanQueueDelivery.recorders`.
- Config: optional `delivery_log: {enabled: true, keep_days: 60, result_patterns:
  {delivered: [...], not_delivered: [...]}}` (`DeliveryLog` with `delivered_patterns` and
  `not_delivered_patterns`).
- Console: page `/deliveries`, `GET /api/deliveries`, `GET /api/deliveries/summary`.
- Inbox CLI: `deliveries [--lane L] [--hours N] [--status S] [--json]`.

## P19 console undo

All additive; the schema version stays 2.

- Ledger table created by `migrate()`: `console_actions(id INTEGER PRIMARY KEY, at TEXT,
  action TEXT, ids TEXT, before TEXT, after TEXT, by TEXT, undo_until TEXT, undone_at TEXT,
  result TEXT, meta TEXT)`; `migrate()` adds missing columns to an earlier table. `before`
  and `after` are JSON lists of `{id, state, lane, note, done_at, ack_at, assigned_at,
  route_reason}` (`SNAPSHOT_FIELDS`); `meta` is `{lane, wake_requests: [ids],
  channel_exclusions: [ids]}`.
- `LedgerStore` adds `message_snapshots(ids)`, `open_channel_ids(channel, *, source)`,
  `wake_request_mark()`, `channel_exclusion_mark()`, `wake_requests_after(mark, ids,
  source)`, `channel_exclusions_after(mark, by)`, `add_console_action(*, at, action, ids,
  before, after, by, undo_until, meta=None)`, `get_console_action(id)`,
  `list_console_actions(limit=20, *, pending_only=False)` and `undo_console_action(id, *,
  at, now_epoch, by="console")` (raises KeyError or `UndoStateError` with `code` `undone`
  or `expired`). `undo_console_action` deletes the action's unconsumed `wake_requests`
  rows of restored ids and sets `removed_at` on its still active `channel_exclusions` rows.
- `console.actions`: `Journal(store, action, *, lane=None, by="console")` with `watch(ids)`,
  `channel_items(message)` and `finish(undo_seconds, now)`; `apply(..., journal=None)`;
  `undo(store, action_id, *, now, by)`, `undo_last`, `last_undoable(store, now)`,
  `undoable(row, now)`, `seconds_left(row, now)`, `stamp_at(epoch)`, `undo_text(result)`,
  `undo_main(argv=None, *, store=None, stdout=None, stderr=None, clock=time.time)` and the
  module entry point `python -m slack_dispatch.console.actions undo [--last | ACTION_ID]
  [--json]`.
- `console.queries.recent_actions(store, view, now, *, limit=20)`.
- Console: `POST /api/queue/actions`, `POST /api/queue/commit` and `POST
  /api/lanes/{lane}/actions` responses add `undo`; `GET /api/config` adds `undo_seconds`;
  new `GET /api/actions`, `POST /api/undo/last` and `POST /api/undo/{action_id}` (JSON body
  `{}`).
- Config: optional `console.undo_seconds` (integer, default 30, minimum 10).
- fix1: `Journal` never raises (`failed`, `status`); write responses add `journal` (`ok` or
  `failed`). `LedgerStore.binding_snapshots(ids)`; journal entries may carry `binding`
  (`{source, channel, thread_ts, lane}`), restored on undo or reported `binding_conflict`;
  undo results add `nothing_restored` (exclusions kept when true), per-id `binding` and
  counts `binding_restored`, `binding_conflict`. `actions.undo_command(store, *, last,
  action_id, as_json, stdout, stderr, now, by="cli")`. Inbox CLI: `undo [--last |
  ACTION_ID] [--json]` (`UNDO_CLOCK`), and `inbox_rows` skips rows whose ledger lane is null
  and state is `human_pending` or `excluded`.

## P20 display names and permalinks

All additive; the schema version stays 2 and no table changes.

- `slack_dispatch.console.queries`: `DisplayNames(config=None, store=None)` with `user(id)`,
  `user_name(id)`, `channel(id)`, `channel_name(id)`, `directory()` and `membership()` (one
  read of `user_directory` and `channel_membership` per instance); `GROUP_DM`;
  `message_permalink(domain, channel, ts, thread_ts=None)` (built on
  `notify.dispatch_card.permalink`); `workspace_domain(config)`. `View(config, store=None)`
  adds `bind(store)`, `user_name`, `channel_name`, `permalink`, `domain` and `names`;
  `membership_view(store, view=None)`; `delivery_log_start(store, view, now, *, hours, lane,
  purpose, status, empty)`; `action_links(view, ids, refs)` and `RECENT_LINKS`.
- Console payloads add `author_name`, `channel_name` and `permalink` (queue items, roots,
  context, search rows), `permalink` on Deliveries items and on lane inbox roots and items
  (with a domain), `channel_name` on lane inbox items, `links` on `GET /api/actions` rows,
  `permalinks` on `GET /api/config`, and `log_started_at`, `log_started_when` and
  `legacy_deliveries` on `GET /api/deliveries`. `{id, name}` objects and `GET /api/owner`
  are unchanged.
- `LedgerStore` adds reads `distinct_authors(*, source)`, `authors_without_directory(*,
  source)`, `message_refs(ids)`, `delivery_log_started_at()` and
  `count_service_deliveries(*, since, before, lane)`. `migrate()` inserts the `cursors` row
  (`meta`, `delivery_log_started_at`) once (`META_SOURCE`, `DELIVERY_LOG_STARTED`).
- `slack_dispatch.ingest.user_directory`: `parse_user` prefers `real_name`;
  `UserDirectory.ensure_name(user)`, `refresh(user)` and the counter `names`;
  `open_reconcile_client(config)`.
- `slack_dispatch.pipeline`: `Pipeline.resolve_names(now=None, *, limit)`,
  `resolve_names_enabled()`, `directory_names` and `name_clock`; `flush` calls
  `resolve_names` after releasing the pipeline lock (fix1); `NAME_LOOKUPS_PER_FLUSH = 20`,
  `NAME_LOOKUP_BUDGET_SECONDS = 3.0`, `NAME_TRACK_LIMIT = 5000`. Heartbeat counter
  `directory_names` with the directory. `UserDirectory.ensure_name` calls users.info without
  the directory lock (`_request`).
- Inbox CLI: `show_inbox(..., names=None, links=None)`; `display_config`, `display_names`,
  `permalink_for`; `inbox --links`; `directory refresh [--missing-only] [--json]`;
  `main(..., client=None)`; `NAME_COLUMNS`, `text_label`. Text output keeps its columns and
  appends `channel_name author_name` (`inbox`) or the two names (`queue`, after `id suggestion
  reason`); spaces become `_` (fix1).
- Config: optional `console.workspace_domain` (Slack subdomain, default null) and
  `user_directory.resolve_names` (bool, default true).

## P21 lane sessions and wake now

All additive; the schema version stays 2 and no table changes.

- Config: optional `console.sessions_table` (non-empty string path, default null).
- `slack_dispatch.notify.session_resolve`: `parse_sessions_table(text)` returns `{session id
  (lower case): {id, title, state, last_used, size}}`; `SessionTable(path)` with `rows()`
  (parsed again only when mtime or size changes; missing, unreadable or over
  `TABLE_MAX_BYTES` reads empty), `lookup(value)` (full id or unique prefix) and `parses`;
  `RESUME_CELL`, `NO_SESSION`, `PAUSED`. `SessionResolver(..., sessions_table=None)` (a
  path or a `SessionTable`, kept as `table`) adds `title(value)` and `describe(value, *,
  paused=False)` returning `{id8, id, value, addressed_by, name, live, title, state,
  table_state}`; `describe` neither logs nor changes `lane_state`, `resolved` or the
  skipped counter. The service and its adapters are unchanged (no table).
- `slack_dispatch.console.queries`: `session_identity(resolver, session, paused=False)`,
  `session_titles(resolver)` (None without a table), `wake_refusal(config, store, resolver,
  lane)` returning `(code, reason)` or None with codes `unregistered`, `paused`,
  `no_session`, `not_live`, `ambiguous`, `wake_off` (`WAKE_REFUSALS`),
  `lane_wake_info(config, store, resolver, lane)`, `lane_identities(store, config,
  resolver)`, `dm_partner(view, kind, key)`. `lane_card` adds `identity` and `wake`;
  `queue_item` adds `is_dm`; proposals and learned rows add `partner`;
  `owner_lookup(..., titles=None)` adds `session_title` per candidate and
  `deliveries_page(..., titles=None)` / `delivery_row(..., titles=None)` add `target.title`,
  both only when `titles` is given.
- Console HTTP: `POST /api/lanes/{lane}/wake` (`{ids?}`, 1 to 200 ids; 400 unknown lane, 404
  lane not in the ledger, 409 refused with the reason as `detail`); `GET /api/lanes/{lane}/inbox`
  adds `identity` and `wake`; `GET /api/queue` adds `lane_sessions`.
- `slack_dispatch.inbox.cli`: `wake_candidates(store, lane)`, `wake_check(store, lane, id)`,
  `wake_lane(store, root, lane, ids=None, *, source, undo_seconds=30, now=None)` returning
  `{lane, queued, assigned, acked, results, wake_requests, undo, journal}`,
  `journal_wake(...)`, `wake_text(result)`, `wake_command(...)`; command `wake LANE [IDS...]
  [--json]`; `WAKE_STATES`, `WAKE_ACTION = "lane_wake"`, `WAKE_CLOCK`, `DEFAULT_UNDO_SECONDS`.
- Journal: action `lane_wake`, `by` `console` or `cli`, `before` equal to `after`, `meta`
  `{lane, wake_requests, channel_exclusions: []}`; undone by the unchanged
  `LedgerStore.undo_console_action`.
- fix1: `wake_requests` gains `kind TEXT NOT NULL DEFAULT 'assign'` (added by `migrate()`;
  `WAKE_KIND_ASSIGN`, `WAKE_KIND_NOW = "wake_now"`). `LedgerStore.request_wake(lane,
  message_id, source, kind=None)` and `pending_wake_ids(lane)`. `service.consume_wake_requests`
  offers a `wake_now` request for an `assigned` or `acked` item and keeps the id in
  `Pipeline.wake_now_ids` (memory) while its wake is deferred or batched; `assign` requests
  stay assigned-only. `Pipeline._current_ids` lets an acked lane id through only while it is
  in `wake_now_ids`; `Pipeline.flush` drops each flushed batch from the set.
- fix1: `wake_lane(..., limit=None)` (default `WAKE_LIMIT = 20`, newest first) adds `total`,
  `capped`, `already_queued`, `records_written` and `records_skipped`; explicit ids with a
  pending request are `ok` with detail `already_queued`. `backfilled_ids(root, lane, ids)`;
  `journal_wake(..., before=None)` stores the pre-wake snapshots as `before`.

## P22 followup wakes

- Config: `wake_policy.followup` in `changed_after_ack` (default), `always`, `never`
  (`config.schema.FOLLOWUP_POLICIES`, `WakePolicy.followup`); any other value is a
  `ConfigError`.
- `LedgerStore.record_event` returns `RecordResult.previous_body`: for a `message_changed`
  event of an existing row, the stored body before the update (None when no body was
  stored or store_body is off); None for every other event and for duplicates. The update
  itself (`edited_at`, `followup=1`, body when store_body) is unchanged.
- `slack_dispatch.pipeline`: `edit_text_changed(previous_body, text)` (whitespace-trimmed
  comparison; a missing value counts as changed), `followup_policy(config)` (default
  `changed_after_ack` when the key is absent), `FOLLOWUP_READ_STATES = ("acked", "done")`.
  `Pipeline._edit_verdict(mid, message, recorded, event)` returns `EDIT_IGNORE`,
  `EDIT_FIRST` or `EDIT_WAKE` (fix1; replaces `_edit_wakes_lane`). `EDIT_FIRST` (any edit of
  a `new` item; a text edit of a `human_pending` item under `changed_after_ack`) routes as a
  first assignment outside the edit counters and window, and a re-routed item that stays
  `human_pending` gets no second queue notice (counted in `edits_ignored`). `EDIT_WAKE`
  routes with the followup inbox record and wake offer as before P22. An ignored edit is
  recorded only and returns `HandleResult(state=<current>, lane=<current>)`. `Pipeline.edits_ignored` and `Pipeline.edits_woken` (ints) appear in
  `snapshot()` (heartbeat counters) once either is nonzero. The per-id edit wake times are
  process memory (`Pipeline._edit_wakes`), so a restart forgets the window.
- Inbox records, delivery log kinds, reply followups and the `message_deleted` path are
  unchanged.

## P23 catch-up wakes

- Config: `wake_policy.catchup_minutes` (non-negative integer, default 30, 0 disables;
  `WakePolicy.catchup_minutes`). A negative value, a boolean or a non-integer is a
  `ConfigError`. Configs without the key load unchanged with 30.
- `slack_dispatch.pipeline`: `CATCHUP_MINUTES_DEFAULT = 30`, `catchup_minutes(config)` (the
  default when the value is absent or invalid) and `catchup_recent(config, ts, now)`
  (inclusive, absolute distance between the Slack ts and `now`; False for a window of 0 or
  an unreadable ts).
- `Pipeline._handle`: a non-duplicate message of origin `reconcile` that routes to a lane
  and is `catchup_recent` gets its inbox record and then `wake_engine.offer` like a socket
  message (Deliver, Defer and Batch, storm notices included). `HandleResult` adds
  `catchup: bool = False` (true for such an offer); `backfill` stays true only for a
  reconcile message that reached a file inbox without an offer.
- `lane_kind(store, ids, catchup=())`: a reconcile id in `catchup` counts as `catchup`; a
  wake of reconcile ids only is `catchup` when any is one, else `backfill`; `reroute` still
  wins and any live id makes it `new`. `Pipeline.catchup_ids` (insertion-ordered dict, memory
  only, capped at `NAME_TRACK_LIMIT`) holds the offered ids until their lane wake was
  attempted (ids waiting in the retry queue keep the mark).
- `Pipeline.catchup_woken` and `Pipeline.backfill_silent` (ints) appear in `snapshot()`
  (heartbeat counters) once either is nonzero.
- `SlackReconcileSource`: the pass summary dict gains `catchup_wakes` (callback results
  with `catchup is True`) and the `Reconcile pass` line ends with `; catchup_wakes=N` after
  `backfill delivered N (no wake)`; the console `reconcile_summary` parses it as
  `counts["catchup_wakes"]`.
- Inbox record kinds, the duplicate path, human notices and the prehandled and owner post
  paths are unchanged.

## P24 edit and reroute guards

- `LedgerStore.record_event` returns `RecordResult.previous_state` and
  `RecordResult.previous_lane` (additive, default None): for a `message_changed` event of
  an existing row, the row's state and lane before the update; None for every other event,
  for unknown rows and for duplicates. An edit still updates only `edited_at`, `followup`,
  `has_files`, `is_bot` and the body; state, lane and route_reason are never touched.
- `slack_dispatch.ingest.normalize.comparable_text(value)`: one comparison form for a
  stored body and an edit text (`<url|label>` -> `url`, `<@U|name>` -> `<@U>`,
  `<#C|name>` -> `<#C>`, `<!...>` dropped, `&lt;`, `&gt;`, `&amp;` decoded, word-boundary
  spans `*x*`, `_x_`, `~x~`, inline code and code fences unwrapped, line-leading bullets and
  `>` dropped, whitespace collapsed; fix1: markers inside a word such as `5*3` or
  `snake_case` stay); None for a
  non-string. Nothing is stored in this form.
- `slack_dispatch.pipeline`: `edit_text_changed` compares `comparable_text` of both sides
  (a missing value still counts as changed). `LANE_HELD_STATES = ("assigned", "acked",
  "done")`, `edit_state(message, recorded)` (previous_state, else the row state) and
  `edit_holds_lane(message, recorded)`. `_edit_verdict` reads `edit_state`. In `_handle` a
  `message_changed` event of an item that held a lane never reaches `route` or
  `apply_decision`: `EDIT_FIRST` becomes `EDIT_IGNORE`, and `EDIT_WAKE` runs
  `Pipeline._redeliver_edit` (state `assigned` in the current lane, lane and route_reason
  kept, followup inbox record, wake offer, edit counters and window, attention flag and
  notice from the edited text; returns `HandleResult(state="assigned", lane=<current>)`).
  `human_pending` and `new` items, and rows without a lane, keep the P22 paths. fix1:
  `edit_redeliverable(message)` is False for owner posts, prehandled items and context
  rows (route_reason or note), whose `EDIT_WAKE` also becomes `EDIT_IGNORE`;
  `_redeliver_edit` calls `_hydrate(event)` and adds 1 to `counters["routed"]`.
- `slack_dispatch.console.actions`: `thread_key(message)`, `is_thread_root(message)`,
  `thread_items(store, message, lane)` (open item ids of the thread in `lane`, context rows
  and owner posts left out), `bind_message_thread(..., only_unbound=False)` (returns True
  when it bound). `reroute(..., thread=False)`: the binding moves only for a thread root or
  with `thread`; with `thread` the other open items of the thread in the old lane are
  rerouted too (reason `reroute:<their lane>`, `reroute` record, one wake request each).
  `assign` of a reply binds only an unbound thread. `apply(..., thread=False)` passes
  `thread` to `reroute` and lets the journal watch the extra items.
- Inbox CLI: `reroute ID --to LANE [--thread]`.
- Console (fix1): `LaneActionRequest.thread: bool = False`; `POST /api/lanes/{lane}/actions`
  passes `thread=request.thread and request.action == "reroute"` to `actions.apply`. The
  console page does not send it yet.

## P25 operator queue

- `slack_dispatch.queue.human`: `QUEUE_KINDS = ("undecided", "direct")`,
  `DIRECT_STATES = ("assigned", "acked")`, `direct_rows(store, direct_lane)` (open items of
  the direct lane, oldest first, context rows and owner posts left out; [] without a
  lane), `operator_queue(store, direct_lane)` (`{"undecided": [...], "direct": [...]}`),
  `queue_counts(store, direct_lane)` (`{"undecided", "direct"}`) and
  `digest_counts_text(counts, direct_lane=None)`. Existing functions are unchanged.
- `slack_dispatch.console.actions`: `DIRECT_STATES`, `ALREADY_DIRECT`,
  `is_direct_item(message, direct_lane)`, `operator_guard(direct_lane)` (human_pending or an
  open item of the direct lane, else `conflict` `state is <state>`). `apply(...,
  direct_lane=None)`: with a direct lane, `assign` of a direct-lane item reroutes it and
  `direct` (or an assign to the direct lane) raises `ActionError("invalid", "already in the
  direct lane")`. `queue_guard` is kept unchanged; the console now uses `operator_guard`.
- `slack_dispatch.console.queries`: `operator_name(config)`, `direct_session(store,
  config)`, `QUEUE_FILTERS`, `queue_rows(store, config)`, `queue_counts(store, config,
  pairs=None)`, `direct_lane_info(store, config)`; `queue_item(..., queue_kind="undecided",
  session_attached=False)` adds `queue_kind`, `lane`, `direct_session`; `queue_page(...,
  kind="all")` adds `kind`, `counts`, `direct_lane`, `direct_session` (ValueError for an
  unknown kind); `pending_ids` and `registry_closed` cover both kinds. `lane_cards` adds
  `direct` and `listed` per card.
- Console HTTP: `GET /api/queue?kind=all|undecided|direct`, `GET /api/queue/counts`
  (`{undecided, direct, total}`), `GET /api/config` adds `operator_name`, `GET /api/lanes`
  adds `direct_lane` (`{lane, session_attached, listed, queue_count}` or null).
  `POST /api/queue/commit` and `POST /api/queue/actions` use `operator_guard` and pass
  `direct_lane`; request shapes are unchanged.
- Inbox CLI: `queue [--kind all|undecided|direct] [--counts]` (no `--kind` = both kinds),
  `QUEUE_KIND_CHOICES`, `QUEUE_LIMIT = 100` (per kind, the pre-P25 cap), `queue_command`,
  `queue_direct_lane`, `queue_line`; undecided lines unchanged, direct lines `id
  direct:<lane> <state> channel author`, a last line `# Operator queue: ...` only with
  `--kind` or `--counts` (fix1).
- fix1: `pipeline.HumanQueueDelivery._send` appends `digest_counts_text(queue_counts(store,
  lane), lane)` as the last line of a digest when `console.queries.direct_lane(config)` is
  set (lazy import); immediate notices and cards are unchanged. `operator_queue(store,
  direct_lane, limit=None)` and `direct_rows(store, direct_lane, limit=None)` take a per
  kind cap. Console: `queue_page(..., session_live=None)` adds `direct_session_live` and item
  `direct_session_down`; lane cards add `session_down`; `direct_lane_info(store, config,
  session_live=None)` adds `session_down`.
- Dispatch cards: eligibility unchanged (`human_pending` only); documented for direct-lane
  attention items.
- No ledger schema change and no new store method.

## P26 mention-only channels

All additive; no ledger schema change.

- Config: a `channel_defaults` entry takes an optional boolean `mention_only` (default
  false): `{default: <lane or null>, mention_only: true}`. A non-boolean value and the key
  in a `dm_defaults` entry raise `ConfigError`. `Config.channel_defaults` stays
  `dict[str, str | None]`; `Config.mention_only_channels: tuple[str, ...] = ()` lists the
  flagged channel ids.
- Router: `slack_dispatch.router.engine.NO_MENTION = "excluded:no_mention"` and
  `is_mention_only(message, config)`. In a mention-only channel a message without
  attention that is not a reply in a bound thread yields
  `Decision(None, "excluded:no_mention", "excluded")`; a bound reply yields the
  `thread_inherit` decision; a message with attention follows the normal order. Under
  `bot_messages.policy: route_or_exclude` a bot message in a mention-only channel yields
  `excluded:bot`. `route()` signature unchanged.
- Pipeline: the excluded decision goes through `apply_decision` as before (state
  `excluded`, note `excluded:no_mention`); `Pipeline.excluded_no_mention` (int) appears in
  `snapshot()` (and so the heartbeat) once nonzero.

## P27 owner reply closes direct items

All additive; no ledger schema change and no new config key.

- `LedgerStore.owner_reply_items(source, channel, root, direct_lane=None)`: rows of the
  thread (the root included), oldest first, without bodies, that are `human_pending` or,
  with `direct_lane`, `assigned` or `acked` in that lane. `pending_in_thread` is unchanged.
- `slack_dispatch.pipeline`: `owner_reply_enabled(config)` (`owner_resolver.enabled` and
  `close_queue_on_owner_reply`), `owner_reply_targets(config, store, source, channel, root,
  reply_ts)` (ids with a ts lower than `reply_ts`; the direct lane is
  `console.queries.direct_lane(config)`, lazy import; owner posts, context rows and
  prehandled items left out; a store without `owner_reply_items` falls back to
  `pending_in_thread`), `close_owner_reply_items(store, ids)` (state `done`, note
  `owner_reply`, unsent `human_notices` rows deleted; returns the count).
  `Pipeline._close_on_owner_reply` uses them; it still runs only for an owner thread reply
  (not a root post) and rewrites the queue export when something closed.
  `owner_reply_closed` counts every closed item of both kinds.
- Inbox CLI: `sent CHANNEL TS --lane LANE --thread ROOT_TS` (a reply, also a DM thread
  reply) with a config where `owner_reply_enabled` holds and `LANE` is the direct lane
  calls `close_on_operator_reply(config, store, channel, ts, root, lane, stdout, stderr)`
  after the send is first recorded: same targets and close as the pipeline, then prints
  `owner_reply closed=N` (followed by the comma-separated ids when N > 0) and rewrites
  `<service.state_dir>/human_queue.jsonl` when something closed (a failed write prints
  `Queue export failed class=<name>` to stderr; the exit code stays 0). Without a config,
  with the switch off or for another lane nothing changes and no line is printed. The
  service heartbeat counter does not include closes made by the CLI.

## P28 deliveries content

- `GET /api/deliveries` row items add `channel_name`, `author_name`, `when`, `attention`,
  `preview` (first 120 characters of the stored body, whitespace collapsed;
  `[no body stored]` when absent) and `preview_truncated`. Existing item keys are unchanged.
- `slack_dispatch.console.queries` adds `PREVIEW_CHARS`, `NO_BODY_STORED`,
  `delivery_preview(body)`, `delivery_messages(store, ids)` (one query per 500 ids through
  the store's `_rows`, else `get_message` per id) and `prefetch_delivery_items(store, view,
  ids, cache)`. `delivery_item` and `delivery_row` keep their signatures; `deliveries_page`
  prefetches the items of the whole page once.
- Inbox CLI `deliveries --json` items carry the same added keys (text output unchanged).
