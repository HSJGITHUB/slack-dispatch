# Changelog

All notable changes are documented here using the Keep a Changelog format.
This project follows Semantic Versioning while remaining pre-alpha.

## [Unreleased]

### P26 mention-only channels
- `channel_defaults` entries take an optional `mention_only: true` next to `default`
  (default false; the loader rejects a non-boolean and the key in `dm_defaults`). For
  channels of other teams where the operator only wants posts that mention them.
- In a mention-only channel a post with attention (owner or mention user mention, or an
  `always_human` author) routes as before (to the channel default with the attention
  flag); a reply in a bound thread inherits as before; every other post is recorded as
  `excluded` with note `excluded:no_mention`: no lane, queue entry, human notice, inbox
  record or wake.
- Bot posts in a mention-only channel are `excluded:bot` under `route_or_exclude`.
- `Config.mention_only_channels`, `router.engine.NO_MENTION`, heartbeat counter
  `excluded_no_mention` (present once nonzero). Precedence table in `docs/ROUTING.md`
  gains the row.

### P25 operator queue
- The console queue is the operator queue: `human_pending` items (`queue_kind` `undecided`)
  plus the `assigned` and `acked` items of the direct lane (`console.direct_lane`,
  `queue_kind` `direct`), newest first. Live case: the queue showed 0 while 16 direct-lane
  items waited in the Lanes tab.
- The tab label counts both (`큐 (N)`), the header shows the split (`레인 결정 필요 N · <operator>
  직접 N`) and a filter toggle shows one kind or both. `GET /api/queue?kind=`, `counts`,
  `GET /api/queue/counts`, `operator_name` in `GET /api/config`.
- Same row verbs for both kinds with staging and GO: `Assign` on a direct row reroutes it
  (journaled, undoable), `Direct` is hidden and refused for a direct row, `Done` and
  `Exclude` as before.
- Lanes tab: without a session the direct lane card is replaced by one line linking to the
  queue; with a session attached the card is back and queue rows say `session attached`.
- Inbox CLI `queue --kind all|undecided|direct` with direct rows; a counts line only with
  `--kind` or `--counts`, and the 100 row cap kept (fix1). `queue.human` helpers
  `operator_queue`, `queue_counts`, `digest_counts_text`.
- fix1: human queue digests end with `Operator queue: undecided=N, direct=M (lane X)` when a
  direct lane is configured. Card eligibility, immediate notices and the heartbeat
  `human_pending` counter are unchanged.
- fix1: an attached direct lane session that is not live shows `session down` (`세션 꺼짐`) on
  the lane card and on direct queue rows.

### P24 edit and reroute guards
- An edit (`message_changed`) of an item that already has a lane (`assigned`, `acked` or
  `done`, rerouted or assigned by hand included) is never routed again: lane,
  route_reason and the thread binding stay. The P22 wake case re-delivers to the item's
  current lane as a followup instead of a recomputed lane. Live case 2026-11-16: a link
  preview edit of a reply rerouted from lane A to lane B was routed back to lane A.
- Edit text comparison compares like with like: the stored body and the edit text are
  reduced to one form (`comparable_text`: link and mention targets, entities, word-boundary
  formatting markers, whitespace), so a link preview edit without blocks is no text change.
- `RecordResult.previous_state` and `previous_lane`: the gate uses the state before the
  edit was recorded.
- `reroute` of a reply moves that reply only and leaves the thread binding; a root
  reroute moves the binding. New CLI flag `reroute --thread` (console API: `"thread": true`
  in the lane action body) moves every open item of the thread in the old lane and the
  binding.
  `assign` of a reply binds only a thread without a binding.

### P23 catch-up wakes
- Backfill that a reconcile pass finds within `wake_policy.catchup_minutes` (new, default
  30, 0 disables) of its Slack ts, such as a reply posted while the service restarted, is
  offered to the wake engine after its inbox record like a live post. Debounce, storm and
  wake switch rules apply; the inbox record keeps kind `backfill` and the delivery log row
  has the new kind `catchup`. Older backfill still fills the inbox without a wake.
- The socket event of the same message that follows stays a duplicate (no second wake);
  human queue and attention notices are unchanged and prehandled items never wake.
- `HandleResult.catchup`, heartbeat counters `catchup_woken` and `backfill_silent` (present
  once either is nonzero) and `; catchup_wakes=N` at the end of the reconcile pass line;
  `backfill delivered N (no wake)` counts only silent backfill.

### P22 followup wakes
- Edits no longer wake a lane for every `message_changed` event. New `wake_policy.followup`:
  `changed_after_ack` (default) wakes only for a text change of an item the lane already
  acked or closed, at most once per `service.followup_window_seconds` per message;
  `always` keeps the old behaviour; `never` records edits only.
- A link preview attaching (text unchanged) or an edit of an item the lane has not read yet
  is recorded in the ledger (body, `edited_at`) with no routing, inbox record, wake offer
  or human notice; the item keeps its state and lane.
- `RecordResult.previous_body` exposes the stored body before an edit. Heartbeat counters
  `edits_ignored` and `edits_woken` (present once either is nonzero) and one INFO log line
  with the message id per edit recorded without a wake.
- fix1: an edit of a `new` item (never routed) is routed as a first assignment under every
  policy; a text edit of a `human_pending` item is routed again (a first assignment when it
  now reaches a lane, ledger only with no second queue notice when it stays queued).
  SERVICE.md states the lane and attention limits and the `store_body: false` limit.

### P21 lane sessions and wake now
- Optional `console.sessions_table`: a Markdown table (`| last used | state | title | size |
  `claude --resume <uuid>` |`) read only and cached by mtime. `SessionResolver.describe`
  returns the session's title, live name, id8, liveness and state without logging.
- Console Lanes cards and the lane inbox header show `Session: <title> (<live name>, <id8>)`
  or a `no session`, `paused` or `session off` badge; queue lane buttons get the session in a
  tooltip; Deliveries targets and owner candidates show the title (only with the table, so
  payload shapes are unchanged without it).
- Wake now: `POST /api/lanes/{lane}/wake` (`{ids?}`, default every assigned and acked item)
  writes a `backfill` inbox record and a wake request per item, journaled as `lane_wake` so
  P19 undo removes unconsumed requests; refused (409) while the lane is paused, has no live
  session or wakes are off. Lane header `Wake now` and per-row `Wake this item` buttons;
  inbox CLI `wake LANE [IDS...] [--json]` with the same rules.
- fix1: `wake_requests.kind` (`assign` default, `wake_now`) lets the service wake acked items
  for Wake now only (`Pipeline.wake_now_ids` carries debounced ones through the flush). The
  default selection wakes the newest 20 items (`capped`, `total`), skips items with a pending
  request (`already_queued`), writes a `backfill` record only once per item, and the undo
  journal keeps the pre-wake snapshot.
- DM items: queue rows of `im` conversations show a `DM` badge; the Scope tab lists DM
  proposals and DM claims under their own headings with the partner's display name. Routing
  is unchanged; docs describe the `scope add --dm` proposal and Scope tab confirmation.

### P20 display names and permalinks
- One name resolver, `console.queries.DisplayNames`: users by config `people`, then the
  ledger `user_directory.name`, then the id; channels by `console.channels`, then the ledger
  `channel_membership` (`#name`, `DM:<partner name>` for direct messages, `group DM` for
  group DMs), then the id. The console reads each table once per request; ids stay on hover.
- Console rows (triage, lane threads, Deliveries item lists, search, Scope names) show these
  names; the payloads add `author_name`, `channel_name` and `permalink`.
- `console.workspace_domain` (a Slack subdomain) turns on message permalinks: a small link
  icon on every message row (new tab), the thread root line links to the root, and Recent
  actions link their first ids. The console and the CLI build links with the dispatch
  card's own helper plus `?thread_ts=<root>&cid=<channel>` for replies.
- CLI: `inbox`, `queue`, `owner` and `deliveries` use the same name order. Text output keeps
  its columns (channel ids) and appends `channel_name` and `author_name` (spaces as `_`);
  scripts should use `--json`. `inbox --links` adds a `link` column and `--json` carries
  `permalink` when the domain is set.
- `user_directory.resolve_names` (default true): the service looks up every new author (not
  only app posts) from its flush outside the pipeline lock, at most 20 and 3 seconds per
  flush (queue and checked map capped at 5,000 users), with the P16 TTL, cooldown and
  fail-open rules; heartbeat counter `directory_names`. `users.info` names now prefer
  `real_name`. Operator backfill: `directory refresh [--missing-only] [--json]`.
- Deliveries tab: an empty window shows when the log started (stored once at migration)
  and how many wakes the older `service_deliveries` record holds for the window before
  that start (no details).

### P19 console undo
- Every console write that changes message rows (triage `Apply now`, a GO commit as one
  row, lane `ack`, `done` and `reroute`, a channel exclusion with every item it closed) is
  journaled in the new ledger table `console_actions` with per-id before and after
  snapshots (state, lane, note, done_at, ack_at, assigned_at, route_reason).
- `POST /api/undo/{id}` and `POST /api/undo/last` restore the before fields within
  `console.undo_seconds` (default 30, minimum 10). An id whose state or lane changed
  meanwhile is reported as `conflict` and left alone. Undoing an assign or reroute removes
  its unconsumed wake requests and reports `wake_sent` when the wake already went out;
  undoing a channel exclusion also ends the exclusion row it wrote.
- Console: a bottom toast with a live countdown after each immediate action or GO (`z`
  undoes while it shows), `Handled · Undo` inline on lane rows that were done or rerouted,
  and a `Recent actions` card in the Service tab (`GET /api/actions`).
- CLI: `undo [--last | ACTION_ID] [--json]` in the inbox CLI and as
  `python -m slack_dispatch.console.actions undo`.
- Undo also returns a thread binding the action changed to its earlier lane (or reports
  `binding_conflict` when it changed meanwhile). When no item is restored the channel
  exclusion stays (`nothing_restored`). A journal failure never changes the action or its
  response (`journal: failed`, one WARNING).
- Inbox `inbox` no longer lists rows without a lane in state `human_pending` or `excluded`
  (an assign undone from the console no longer shows in the lane inbox).

### P18 delivery log
- New ledger table `delivery_log`: one row per attempt to wake a lane session or to reach
  the operator (digest, card, attention, reminder), including the skipped cases
  (`skipped_no_session`, `skipped_ambiguous`, `skipped_paused`), `dry_run`, `failed` and
  `timeout`, with kind, lane, adapter, target session, message ids, cost and duration.
  File inbox appends are not logged. Rows older than `delivery_log.keep_days` (default 60)
  are pruned at start and once a day. Relay rows are stamped with the relay start; repeated
  identical skipped attempts fold into one row (`last_seen_at`, `repeat_count`).
- The session relay's `--output-format json` stdout is parsed (`result`, `is_error`,
  `total_cost_usd`, `duration_ms`, `session_id`) and its result text is classified with
  `delivery_log.result_patterns` as delivered, not_delivered or unknown. Both the old `DONE`
  answer and an echoed SendMessage tool result work; output that is not JSON is kept as
  `unparsed output`. Details never carry a message body or exception text.
- Receipts: a sent lane wake is received at the first `ack` or `done` of its items by that
  lane after the wake; card rows show a returned card decision.
- Console `Deliveries` tab (`/deliveries`, `GET /api/deliveries`,
  `GET /api/deliveries/summary`) and inbox CLI `deliveries [--lane] [--hours] [--status]
  [--json]`.

### P17 owner resolver and registry sync
- `slack_dispatch.resolver`: a read-only index of the session thread registry JSON
  (field aliases `ch`/`channel_id`/`channel` and `root_ts`/`thread_ts`, name strings,
  `-` roots, duplicate rows, session ids from owner texts, `_meta` sweep scopes, routing
  rules), cached per file version; a parse failure keeps the previous index and logs the
  class only. An owner resolver orders candidates by tier: certain (binding, scope claim,
  config default), declared (registry session ids, sweep scope, routing rule), recorded
  (owner text without a session id), observed (ledger activity, optional harvest jsonl).
  Local files and the ledger only; no Slack, model or network calls.
- Human queue items get the resolver's lane as their suggestion (`owner-resolver/1`)
  before the classifier, which now fills only rows the resolver left empty.
- An owner reply in a thread closes the thread's earlier `human_pending` items (note
  `owner_reply`) and drops their unsent notices; lane items are untouched
  (`owner_resolver.close_queue_on_owner_reply`).
- Console: registry badges on queue and lane rows, `Done per registry (stage)` and
  `Stage all registry-closed (N)` for items the registry closed after their ts (staged,
  written on GO), `Find owner` on queue and lane rows and an `Owner lookup` card in the
  Scope tab, backed by `GET /api/owner`.
- Inbox CLI `owner CHANNEL [--ts TS]`, `owner --pending [--apply]` (backfill suggestions)
  and `registry check [--lane LANE]`.
- Config: optional `owner_resolver` section (off by default).

### P16 bot flag and user directory
- One bot rule (`user_directory.is_bot_message`): no author, subtype `bot_message`,
  `bot_messages.known_bot_users`, or a bot per `users.info`. `bot_id`, `app_id` and
  `bot_profile` alone no longer mark a bot, so a human posting through an app gets the
  attention check and reaches the human queue instead of `excluded:bot`.
- The pipeline decides the flag once for socket and reconcile events; the sources and
  the router no longer duplicate it. The router uses the stored flag and never calls
  Slack.
- `UserDirectory` caches `users.info` (new read-only allowlist entry) in the ledger table
  `user_directory` for `user_directory.ttl_seconds` (default 86400), consulted only for
  posts with `bot_id` or `app_id`. A failed lookup keeps an expired cached value, else
  counts the author as human, warns once per user per hour and is not retried for
  `user_directory.failure_cooldown_seconds` (default 300). `user_directory.enabled:
  false` keeps the other three rules. `repair bot-flag` refuses context rows and owner
  posts like `assign` and `cutover`.
- Heartbeat counters `directory_lookups`, `directory_failures`, `app_posts_human`,
  `bot_posts` when the directory exists.
- Inbox CLI `repair bot-flag` (dry run by default) fixes rows the old rule flagged:
  re-routes `excluded:bot` rows as backfill, recomputes attention on open lane items and
  clears the flag elsewhere; owner rows are refused, unknown authors skipped unless
  `--assume-human`.

### P15 membership filter
- Member-only intake: `membership.py` caches `users.conversations` (paginated, through the
  reconcile client) in the ledger table `channel_membership`, refreshed at start and every
  `membership.refresh_seconds`. Under `membership.policy: exclude` (default) events from
  channels the operator is not a member of are recorded as `excluded`
  (`excluded:not_member`) and never routed or queued; `off` keeps today's routing. A
  failed refresh keeps the previous table; an empty cache keeps today's behaviour.
  Configured channels that are not member channels are reported once at startup.
- Reconcile scans member channels only and `discover_channels` reads the cache instead of
  `conversations.list`. Heartbeat counters `member_channels` and `excluded_not_member`.
- Console: `Exclude channel` closes every open item of the channel and writes a
  `channel_exclusions` ledger override that routing honours like `excluded_channels`;
  the Scope tab lists them with undo and an `excluded_channels` YAML export. Queue rows
  show a `not a member` badge (both with Korean labels in `ko`).

### P14 console staging
- Triage decisions are staged, not applied: a click marks the row (chip, highlighted
  button; a second click unstages), and one sticky `GO (N)` applies every staged
  decision through the new `POST /api/queue/commit`, with per-id results `applied`,
  `conflict`, `not_found` or `error` in request order. Undecided rows are untouched.
- Staged decisions persist in browser localStorage by message id, survive polling and
  reloads, and are dropped when the id leaves the queue (`/api/queue` adds
  `pending_ids`). `Cancel all`, a per-row `Apply now` link (the old immediate action),
  and keys `g`, `Enter`, `Esc`. The bulk checkbox bar is removed.
- Add `console.ui_language` (`en` default, `ko`): page strings come from one dictionary
  per language with the same keys, including the Korean verbs and a one-line legend.
- Session wake templates accept `{sent_at}`: render time in `display_timezone` as
  `MM-DD HH:MM:SS`.
- An applied exclude from a channel without a lane default returns `channel_hint` and
  the page suggests the Scope tab. No routing change.

### P13 scope learning
- Add `scope add|list|remove|import` and `sent` to the inbox CLI: lanes register channels,
  threads, DMs and people; thread claims and sends bind threads at once; DM sends add dm
  claims; the harvest proposal YAML loads as suggested proposals and thread bindings.
- Add ledger tables `scope_claims`, `scope_proposals` and `sends`, the router rule
  `ScopeClaim` (confirmed claims only, after thread inheritance and before explicit tags)
  and `scope:auto` for a brand new channel claimed by a single lane.
- Learn from behaviour: reroutes and human assignments of queued items count as evidence;
  proposals become `suggested` after `scope_learning.min_evidence` (default 2) events, with
  conflicts flagged. Nothing routes until the operator confirms.
- Console Scope tab (`/proposals`): confirm, reject or defer proposals; confirmed claims
  take effect without a YAML edit; the Learned scope panel exports a YAML fragment.
- Fix: `assign` and `reroute` (CLI, console, card decisions) move the thread binding to
  the new lane; a `cutover --apply` rerun no longer appends items that already have an
  unconsumed backfill record.

### P12 cutover and cards
- Add `cutover LANE [--seen-before TS] [--note TEXT] [--apply]` (dry run by default):
  close only lane items delivered before the lane's last own ack or done, keep later
  items and every attention item open and deliver them again as `backfill`.
- Add the `dispatch_card` notify adapter: immediate human-queue notices become
  `dispatch_card/1` requests for Jarvis, written through `cards/.tmp/*.part` and a rename;
  digests never produce cards.
- Add the card decision consumer (`decisions` config section): HMAC-verified Jarvis
  decisions assign (`human:card`), assign to the direct lane or exclude, then move to
  `decisions/done/` or `decisions/rejected/`; heartbeat counters `card_decisions` and
  `card_decisions_rejected`.
- Add `wake_requests`: CLI `assign` and `reroute`, console actions and card decisions
  queue a lane wake that the service offers within about five seconds.
- Inbox CLI: `--config` before or after the subcommand; `message_deleted` sets
  `deleted_at`, closes open rows and hides them (`deleted` marker with `--include-done`);
  `assign`, `reroute` and `cutover` refuse context rows and owner posts with exit 2.

### P10 console
- Add the optional operator console `python -m slack_dispatch.console --config PATH`
  (FastAPI and uvicorn through the new `console` extra), bound to 127.0.0.1 by default,
  without login, with `Cache-Control: no-store` and Host/Origin checks on every request.
- Triage view of `human_pending` items, newest first, with body, thread root and context,
  classifier suggestion, attention, origin, age and author lane hints; one-click and bulk
  assign, direct, exclude (`excluded`, note `human:console`) and done, recorded with
  `human:console` exactly where the CLI records `human:cli`.
- Lanes view (session liveness through the session resolver, pause, today's counts, oldest
  unacknowledged age, last delivery and reminder, inbox size) with the CLI thread view and
  ack, done and reroute per item; read-only service view (heartbeat, reconcile passes,
  warnings, ingest gaps, wake switch, adapter dry-run flags); ledger search.
- New optional `console` config section (`host`, `port`, `base_url`, `page_size`,
  `lane_hint_people`, `direct_lane`, `channels`). With `console.base_url`, human-queue
  digests shrink to per-channel counts plus `Open: <base_url>/`, and immediate notices end
  with `Open: <base_url>/#msg=<id>`; without it the notice text is unchanged.
- Additive LedgerStore read helpers for the console; no schema change.
- Windows Task Scheduler template and installer for the console
  (`deploy/windows/task_template_console.xml`, `install_console_task.ps1`).

### P9 shadow follow-ups
- Human-queue session adapters target `template.target` (default `operator`) with no lane
  lookup, claim, session resolution or pause; the human_queue pseudo-lane is no longer needed.
- Throttle not-live and ambiguous session logs to per-lane transitions plus one reminder per
  30 minutes; `wake_skipped_no_session` counts distinct (lane, message id) pairs.
- One wake switch path (`wake_switch_path`) for the builder, the gate and the service engine;
  differing state directories log one startup WARNING.
- Retry permanently failing reply parents once per `permanent_error_ttl_seconds` (default 3600),
  with DEBUG after the first WARNING.
- Inbox kind `followup` now means edited (message_changed); recent replies keep `new` or
  `backfill` with `followup: true`, and the CLI `edited` column follows.
- Inbox CLI: UTF-8 console output, optional `people` names and a `when` column in
  `display_timezone`; JSON adds raw `ts` and `author_id`.
- Add `--rehydrate-bodies DAYS` to fill missing bodies once through the reconcile client
  (at most 40 calls per minute, counts only in logs).
- Human notices end with the configured lane ids for `assign ID LANE`.

### P8 session resolve
- Accept session names, full session ids and 8+ hex id prefixes in `lanes[].session` and `claim`.
- Resolve ids against live session files in the new optional `sessions_dir`, with a 30 second scan cache.
- Skip wakes for ids that are not live or ambiguous; warn on duplicate session names.
- A non-null configured session now wins over the ledger claim; `paused: true` in config always pauses.
- `claim --config` (or `SLACK_DISPATCH_CONFIG`) warns when the configured session wins.
- The pipeline and timer delivery gate uses the same precedence, so a configured session
  wakes its lane with a null ledger pointer and config `paused: true` blocks it.
- A not-live or ambiguous id is not delivered: no delivered count, no timer receipt, retried later.
- Heartbeat counters add `wake_skipped_no_session` when a session_message adapter is configured.
- Reconcile skips reply parents with permanent Slack errors for the process lifetime: one
  WARNING, `permanent_skipped` in the pass summary, no failure, replies cursor not held.

### P7B inbox context
- Collapse repeated inbox records with first-delivery age, kind, edit and attention metadata.
- Add thread context, optional owner reply hints, body controls and JSON views.
- Support multiple ids and lane-scoped thread selection for ack and done.
- Add configurable lane wake messages, origin counts and placeholder-only dry-run logs.

### P7A coverage
- Backfill thread replies with independent cursors, overlap and bound-thread coverage.
- Track socket gaps, worker catch-up, lifecycle logs and degraded heartbeat health.
- Flatten message blocks and file names; retain source origin through ledger v2.
- Record owner posts/reactions and prehandled backfill without delivery.
- Add backfill inbox kind and cached, retryable thread-root context hydration.
- Isolate reply fetch failures per parent; roots and the root cursor never wait on replies.
- Cap bound and ledger thread candidates with `bound_threads_max_age_days` (default 14).
- Start the resident first catch-up without blocking the loop; write a heartbeat at startup.
- Deliver backfill to file inboxes only, without wake or storm accounting.
- Skip root hydration in excluded channels; abandon a root after three failed retries.
- Update heartbeat `last_event_at` from socket events only.

### Added
- Durable human-queue digests with channel counts, capped ids and timer receipts.
- Optional bot traffic policies and service Event bot identity.
- JSONL thread binding import with keep/replace behavior and unknown-lane counts.
- Opt-in read-only reconcile discovery with six-hour refresh and exclusions.
- Keep human-queue notices live while lane wakes are disabled.
- Preserve lane routing for protected authors and mentions, with operator attention.

### Fixed
- Add an indefinite one-minute time trigger with an install-time start boundary so stopped services restart without another logon.
- Render Task Scheduler XML as UTF-16 LE with BOM and a matching UTF-16 declaration.

## [0.1.0] - 2026-11-12

### Added
- Resident service and module entry point with locking, source lifecycle, heartbeat,
- Service pipeline with transient routing text, root thread inheritance, followups,
- Durable per-adapter timer receipts and thread delivery history without changing
- Optional service configuration and kind-independent human queue adapter purposes.
- Offline pipeline, timer and service acceptance tests and service operator guide.
- Deterministic routing with excluded and always-human precedence and reply bindings.
- Suggest-only optional Anthropic adapter with strict response parsing and versioning.
- Human review queue, metadata-only JSONL exports, and manual promotion statistics.
- Optional routing configuration defaults and offline router, classifier and queue tests.
- Routing configuration and integration documentation.
- Locked per-lane metadata inboxes and ledger-backed CLI commands.
- Session command and atomic assistant outbox adapters with configuration support.
- Immediate wake decisions, author debounce, storm batches and a global switch.
- Offline contract-fake tests and inbox operation documentation.
- Standalone runtime locks, atomic heartbeats, control flags and redacted daily logs.
- Windows task template, print-only registration, restart and tagged deployment tools.
- Offline runtime tests and operator documentation. Service integration remains pending.
- Transactional SQLite ledger with versioned migrations, deduplication, routes,
  state changes, reactions, thread bindings, and per-channel cursors.
- Redacted Slack event normalization, immediate Socket Mode ACKs, and read-only
  paginated history reconciliation with Retry-After handling.
- Explicit body storage configuration, source construction, synthetic fixtures,
  offline intake tests, and ingestion documentation.

### Fixed
- Derive deployment manifest keys from the canonical target for Windows short paths and casing variants.
- Compute deployment SHA-256 hashes with disposed .NET file streams without Get-FileHash.
- Preserve the full parent environment and optional overrides in PowerShell runtime tests.
- Verify deployment manifest hashes against Python for text, empty and nested binary files.

## [0.0.1] - 2026-11-11

### Added
- Package scaffold, configuration loader, example, protocols and draft ledger DDL.
- Deterministic first-match iteration and command-line parser skeleton.
- Sanitized metadata-only probe and offline redaction tests.
- Sanitized Windows environment helper with byte preservation, backup and ACL tests.
- Design decisions, CI definition, lint and secret-scanning hooks.
