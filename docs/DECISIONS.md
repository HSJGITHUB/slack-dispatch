# Architecture decisions

## ADR-001: Socket Mode plus reconciliation

Date: 2026-11-11

Status: Accepted for the target design; implementation pending.

### Context
Session-local polling can miss old-thread replies and split cursor ownership.

### Decision
Use Socket Mode user events as primary intake and reconcile polling at startup and periodically.

### Consequences
Visibility and permissions require live human validation; durable cursors and idempotent processing remain required.

## ADR-002: Lanes rather than sessions

Date: 2026-11-11

Status: Accepted for the target design; implementation pending.

### Context
Session identifiers change while work ownership persists.

### Decision
Assign to stable lane IDs and store replaceable session pointers.

### Consequences
Claiming a lane preserves its inbox; lane maps and unique session names need explicit maintenance.

## ADR-003: Deterministic first, suggestions second

Date: 2026-11-11

Status: Accepted for the target design; implementation pending.

### Context
Opaque automatic classification makes ownership difficult to audit.

### Decision
Apply thread, tag, channel and person-channel rules in order. Keep classification suggest-only, with future per-channel human-approved promotion.

### Consequences
Record suggestion versions and measured agreement. Promotion needs reversal and protected-message exceptions; self-confidence is insufficient.

## ADR-004: Immediate wakes with burst protection

Date: 2026-11-11

Status: Accepted for the target design; implementation pending.

### Context
Fast delivery is useful but bursts can repeatedly interrupt a session.

### Decision
Use immediate delivery, same-author debounce and a per-lane storm guard that switches to batches.

### Consequences
Provide a global switch and operator notification. Measure wake cost before rollout; paused lanes accumulate quietly.

## ADR-005: Separate resident service and shared human adapter

Date: 2026-11-11

Status: Accepted for the target design; implementation pending.

### Context
Intake reliability should not depend on an interactive session or duplicate human messaging systems.

### Decision
Run a separate resident service; route human DMs through an existing assistant outbox adapter.

### Consequences
Require heartbeat, single-instance protection and recovery; the adapter retains batching and deduplication authority.

## ADR-006: Defer team-visible status

Date: 2026-11-11

Status: Accepted for the target design; implementation pending.

### Context
Public status indicators introduce a social workflow before internal ownership is proven.

### Decision
Start with the internal ledger and defer team-visible reactions or status indicators.

### Consequences
Review observed repeat-pings and operator demand later; no automatic public status writes are authorized.
