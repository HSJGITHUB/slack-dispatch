# Changelog

All notable changes are documented here using the Keep a Changelog format.
This project follows Semantic Versioning while remaining pre-alpha.

## [Unreleased]

### Added
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

## [0.0.1] - 2026-11-11

### Added
- Package scaffold, configuration loader, example, protocols and draft ledger DDL.
- Deterministic first-match iteration and command-line parser skeleton.
- Sanitized metadata-only probe and offline redaction tests.
- Sanitized Windows environment helper with byte preservation, backup and ACL tests.
- Design decisions, CI definition, lint and secret-scanning hooks.
