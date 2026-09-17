# ADR-017: Provider and webhook idempotency

* Status: Accepted · 2026-09-17
* Related: ADR-007 (leases), ADR-012 (outbox), ADR-013 (constraints)

## Context

Escalation delivery is at-least-once: leases expire, workers crash and Twilio redelivers
callbacks. With real telephony, "at least once" must never become "ring the person twice
concurrently" or "record the same callback twice".

## Decision

**Outbound idempotency key = `(scheduled_action_id, attempt)`**, enforced by the partial
unique index `uq_calls_scheduled_action_attempt`. The call row is the persisted *intent*:

1. Phase 1 (one transaction): the executor writes the `Call` row (status QUEUED), the
   `CALL_REQUESTED` ledger event, the timeline entry — and **extends the step's lease past
   the call's maximum duration**, so no healthy competing worker can reclaim a live call.
2. If a competing worker still tries the same attempt (crash-window reclaim), its INSERT
   violates the unique key: it rolls back, counts
   `careos_provider_idempotency_conflicts_total` and skips. **Zero dials** for the duplicate.
3. Only after that commit does the provider talk to Twilio; the returned SID is persisted
   immediately (`bind_provider_sid`).

**Crash windows, deliberately:**

| Crash point | Outcome |
|---|---|
| after phase 1, before the Twilio request | lease expiry → reclaim as attempt N+1 → new call row, one new dial (at-least-once, never concurrent-duplicate) |
| after Twilio accepted, before the SID persisted | the status callback carries our opaque call id in its URL, so the first signed callback re-binds the SID to the same row; a reclaimed attempt N+1 may still dial once more — the accepted residual of at-least-once delivery, bounded by the lease arithmetic |
| between call end and outcome recording | `_record_failure_safely` / lease expiry, unchanged from ADR-012 |

**Webhook idempotency.** Every provider callback has an identity
`status:{CallSid}:{CallStatus}:{SequenceNumber}` (or `gather:{CallSid}`), inserted into the
append-only `call_events` ledger with `ON CONFLICT DO NOTHING` on the partial unique index
`uq_call_events_provider_key`, under a `SELECT … FOR UPDATE` of the call row. Duplicates —
including concurrent ones — commit as no-ops and are answered 204 so Twilio stops retrying.
Late or out-of-order callbacks are recorded but can never move the call backwards
(forward-only rank; terminal states are final). The bound SID must match; org and incident
always come from the call row, never from the request.

## Consequences

* A worker retry cannot place a duplicate real call for the same attempt; the only repeat
  path is an explicit new attempt after lease expiry — visible in `calls.attempt`.
* Tests: `test_voice_escalation_flow.py` (duplicate-attempt skip, retry-as-attempt-2),
  `test_twilio_webhooks.py` (6× concurrent duplicates → one event; replay; SID mismatch),
  plus the DB-level uniqueness test.
