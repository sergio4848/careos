# ADR-012: Transaction boundaries, after-commit side effects and the outbox

* Status: Accepted · 2026-09-17
* Related: ADR-004 (idempotency), ADR-007 (durable escalation), ADR-008 (realtime)

## Context

A technical review asked, for every side effect of an SOS, whether it can be lost, duplicated
or observed before the data it describes exists: incident persistence, timeline, audit,
escalation scheduling, realtime publishing, provider execution, metrics and logs. It also asked
whether a transactional outbox is needed.

Findings in the Sprint 1 code before this decision:

| Side effect | Sprint 1 behaviour | Problem |
|---|---|---|
| Incident, timeline, audit, receipt, escalation schedule | one transaction | correct |
| Realtime publish | after commit, awaited inline | an unexpected broker exception turned a **committed** SOS into HTTP 500 (the device would retry); an unreachable Redis cost ~2 s **per message** (~4 s per SOS) |
| `incidents_created` metric | incremented before commit | a rollback inflated the counter |
| Provider calls | worker, via `scheduled_actions` | correct, but catch-up after worker downtime ran all overdue steps of an incident concurrently, in no defined order |

## Decision

**1. One transaction for everything safety-relevant.** Receipt (idempotency ledger), device
telemetry, incident, every `incident_events` row, audit records and the `scheduled_actions`
escalation schedule commit together or not at all. Tested by crashing after all rows were flushed
and before commit (`test_crash_before_commit_leaves_no_partial_incident_state`): nothing remains,
nothing is announced, no metric moves, and the device's retry with the same `event_id` is
processed normally because the ledger entry rolled back too.

**2. `scheduled_actions` is the transactional outbox for provider work.** Calls, SMS and AI
assistance are never executed in the request. They exist only as rows written in the incident's
transaction and are executed by the worker (lease + `FOR UPDATE SKIP LOCKED`, three-phase
executor, at-least-once). No second outbox table is added: it would duplicate this one.

**3. Realtime and observability are after-commit, best-effort and bounded.**
`UnitOfWork.notify()` and `UnitOfWork.after_commit()` queue work that runs only after a
successful `COMMIT` and is discarded on rollback or commit failure. After-commit work:

* can never raise into the request (exceptions are caught, logged, counted as
  `careos_realtime_delivery_failures_total{stage="after_commit"}`);
* can never delay the response beyond `CAREOS_REALTIME_PUBLISH_TIMEOUT_SECONDS` per message
  (default 1 s);
* can never undo the commit.

Realtime is deliberately **not** put in a durable outbox. A lost notification costs seconds of
freshness, never data: consoles reconcile with the REST API every 30 s while live, every 10 s
otherwise, and refetch everything on reconnect (ADR-008). A durable realtime outbox would add a
table, a relay loop and ordering questions to protect something already protected by
reconciliation.

**4. Redis sits behind a circuit breaker.** One shared `CircuitBreaker` for the realtime broker,
rate limiter and readiness probe. Short Redis timeouts (0.5 s) plus the circuit mean an outage
costs one short wait, then Redis is skipped for 5 s (local socket delivery, in-process rate
limits) before a single probe is allowed through.

**5. Persisted-outcome metrics and "accepted" logs are after-commit hooks.**
`incidents_created`, takeovers, resolutions and `gateway.event_accepted` / `incident.created`
logs are emitted only for committed state.

**6. Escalation steps of one incident run in policy order.** A step is claimable only if no
earlier step of the same incident is running or due. Operator alerts are exempt: bringing in a
human is never queued behind an automated contact attempt.

## Guarantees and non-guarantees

| Component fails | Guarantee |
|---|---|
| Redis down | SOS persisted and escalated; readiness stays `ready` with `redis: unavailable`, `realtime: degraded`; consoles on other API replicas update by reconciliation (≤10 s) |
| Broker raises/hangs | request succeeds within the publish timeout; failure counted and logged |
| AI hangs/raises | deterministic call proceeds; AI session `TIMED_OUT`/`FAILED`; no status change |
| Voice/SMS provider fails | recorded (`CALL_FAILED`/`NOTIFICATION_FAILED`, call row, metrics); retried; operators alerted when exhausted; incident never resolved, closed, rolled back or deleted |
| Worker down | nothing lost: steps wait in PostgreSQL; `/ready` reports `escalation_worker: lagging` and the dashboard shows "Automated escalation is delayed"; on restart steps run in policy order, operator alerts first |
| Worker crashes mid-step | lease expires (60 s) and the step is reclaimed; the call may be repeated (at-least-once) |
| Failure while recording a failure (e.g. DB down) | step stays `RUNNING` and is reclaimed on lease expiry |
| DB down | request fails; nothing partial is written; `/ready` returns 503 |

Not guaranteed: exactly-once provider calls; realtime delivery to every console; escalation
timing better than worker poll interval + lease when a worker crashes.

## Consequences

* Tests: `tests/integration/test_failure_injection.py`, `tests/unit/test_reliability_primitives.py`.
* No new infrastructure (no Kafka, no relay process). PostgreSQL + the existing worker remain
  the only moving parts for safety work.
