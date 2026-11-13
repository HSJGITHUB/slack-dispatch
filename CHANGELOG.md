# Changelog

All notable changes are documented here using the Keep a Changelog format.
This project follows Semantic Versioning while remaining pre-alpha.

## [Unreleased]

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
