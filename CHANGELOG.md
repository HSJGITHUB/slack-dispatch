# Changelog

All notable changes are documented here using the Keep a Changelog format.
This project follows Semantic Versioning while remaining pre-alpha.

## [Unreleased]

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
