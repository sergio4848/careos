# ADR-004: Device event idempotency

* Status: Accepted · 2026-09-16

## Context

Device platforms and alarm receivers retry on timeouts, so the same SOS can arrive several
times, sometimes concurrently. Two incidents for one press would split operator attention;
a lost event is worse.

## Decision

1. Every event carries a producer-generated `event_id` (CareOS Event Contract).
2. The gateway's first write is
   `INSERT INTO device_event_receipts ... ON CONFLICT (organisation_id, event_id) DO NOTHING RETURNING id`
   inside the same transaction that creates the incident. Concurrent duplicates block on the
   unique index until the first transaction commits, then take the duplicate path.
3. Duplicate with the **same canonical payload** (SHA-256 of the normalised event) → `202`
   with `duplicate: true` and the original receipt/incident.
4. Same `event_id`, **different payload** → `409 event_id_conflict`, audited. We deliberately do
   not swallow it: a producer that reuses IDs (e.g. a counter reset after a firmware update)
   would otherwise have a *new, genuine* alarm silently discarded.
5. Rejected events (unknown device, validation failure) roll back, so no receipt is stored and
   a retry after the device is registered can succeed. Rejections are audited.
6. Separately from idempotency, a *new* alarm from a device that already has an active incident
   is attached to it (`ALARM_REPEATED`, priority can only increase), enforced by a partial
   unique index.

## Consequences

* At-least-once delivery from producers becomes exactly-once incident creation.
* Producers must generate unique IDs; the contract documents this.
* Receipts double as evidence (normalised payload) for incident review.
