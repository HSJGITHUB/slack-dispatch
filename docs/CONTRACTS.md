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
record_event(event: Event) -> RecordResult        # RecordResult(message_id: str | None, created: bool, duplicate: bool, kind: str)
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
ThreadInherit -> ExplicitTag -> ChannelDefault -> PersonChannel -> human pending.
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
message reached a file inbox without a wake.

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
