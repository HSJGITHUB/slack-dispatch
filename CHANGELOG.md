# Changelog

All notable changes are documented here using the Keep a Changelog format.
This project follows Semantic Versioning while remaining pre-alpha.

## [Unreleased]

### P7B inbox context
- Collapse repeated inbox records with first-delivery age, kind, edit and attention metadata.
- Add thread context, optional owner reply hints, body controls and JSON views.
- Support multiple ids and lane-scoped thread selection for ack and done.
- Add configurable lane wake messages, origin counts and placeholder-only dry-run logs.

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
