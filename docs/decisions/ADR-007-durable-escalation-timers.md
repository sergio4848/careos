# ADR-007: Durable escalation timers in PostgreSQL (no Celery)

* Status: Accepted · 2026-09-16

## Context

Escalation is a sequence of timed steps (T+0 automated call, T+30s contact #1, ...). A step
must not be lost on a crash, deploy or Redis outage, must not run twice concurrently, and must
stop when an operator takes over or the incident is resolved. The brief suggested Celery or a
better-suited async worker.

Options considered:

| Option | Why not (for now) |
|---|---|
| Celery + Redis/RabbitMQ | sync-first (awkward with async SQLAlchemy); ETA tasks live in the broker, so a broker loss loses timers; revoking scheduled tasks on takeover is unreliable |
| arq / taskiq / dramatiq | async-friendly, but timers still live in Redis and are not transactional with the incident |
| procrastinate (PG-based queue) | closest fit; adds its own schema/CLI outside Alembic; revisit if job types multiply |

## Decision

A small, explicit PostgreSQL-backed scheduler:

* `scheduled_actions` rows are inserted **in the same transaction** as the incident, from a
  **snapshot** of the escalation policy (later policy edits do not change running incidents).
* Workers claim due rows with
  `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING ...`, setting a lease
  (`lease_expires_at`, `locked_by`) and incrementing `attempts`.
* Execution is three short phases: prepare (lock incident, re-check it is active) → provider call
  outside any transaction with a timeout → record outcome (lock incident, apply state machine).
* Expired leases are reclaimed (crash recovery). Provider failures retry with exponential
  backoff; when retries are exhausted a **fail-safe operator alert** is scheduled immediately.
* Takeover/resolution cancel pending steps in the same transaction as the status change.

## Consequences

* No lost or duplicated-concurrently steps; verified by tests for SKIP LOCKED claiming, lease
  reclaim, retries, fail-safe alerting and skip-after-resolution.
* Delivery is at-least-once: a worker crash between the provider call and recording the result
  can repeat a call. For welfare calls this is the safe failure mode.
* Polling interval (default 1 s) bounds scheduling precision; fine for human-scale escalation.
* Throughput is bounded by PostgreSQL; a partial index on `(due_at) WHERE status IN (PENDING, RUNNING)`
  keeps claiming cheap. Re-evaluate at thousands of steps per second.
