# Architecture Decision Records

Short, dated records of decisions that shape CareOS. Format: context, decision, consequences.
A superseded ADR is kept and marked, never deleted.

| ADR | Title | Status |
|---|---|---|
| [001](ADR-001-monorepo.md) | Monorepo layout | Accepted |
| [002](ADR-002-modular-monolith.md) | Modular monolith with two entrypoints | Accepted |
| [003](ADR-003-postgresql.md) | PostgreSQL as the system of record | Accepted |
| [004](ADR-004-event-idempotency.md) | Device event idempotency | Accepted |
| [005](ADR-005-multi-tenancy.md) | Multi-tenancy and tenant isolation | Accepted |
| [006](ADR-006-ai-not-safety-critical.md) | AI is assistive, never safety-critical | Accepted |
| [007](ADR-007-durable-escalation-timers.md) | Durable escalation timers in PostgreSQL (no Celery) | Accepted |
| [008](ADR-008-realtime-delivery.md) | Realtime delivery: WebSocket notifications, REST truth | Accepted |
| [009](ADR-009-server-side-sessions.md) | Server-side sessions instead of JWT for the console | Accepted |
| [010](ADR-010-incident-state-machine.md) | Centralised incident state machine and append-only timeline | Accepted |
| [011](ADR-011-dependency-policy.md) | Dependency and version policy | Accepted |
