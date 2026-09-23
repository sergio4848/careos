# ADR-010: Centralised incident state machine and append-only timeline

* Status: Accepted · 2026-09-16

## Context

Incident status drives what operators see and what automation does. Scattered `if status == ...`
checks in controllers and workers drift apart and allow impossible histories (e.g. CLOSED → OPEN).
Regulators, insurers and safeguarding reviews expect a faithful record of who did what, when.

## Decision

* `careos/modules/incident_engine/state_machine.py` declares every status and its allowed
  targets in one table; `assert_transition` is the only gate. `IncidentEngine.transition` is the
  only function that assigns `incident.status`, and it always appends an `IncidentEvent` with
  `from_status`/`to_status`. As defence in depth, a SQLAlchemy `@validates("status")` hook on the
  `Incident` model re-checks every assignment, so code that bypasses the engine still cannot make
  an illegal change.
* `RESOLVED` (outcome recorded) and `CLOSED` (reviewed, terminal) are distinct. Resolution
  requires a category and notes; closure requires a resolved/false-alarm/cancelled incident.
* `FAILED` (automation failed) can only move towards human handling; it can never be closed
  directly.
* Incident rows are mutated only under `SELECT ... FOR UPDATE`; an optimistic `version` column
  is a second guard.
* `incident_events` and `audit_logs` are append-only: no `updated_at`, no update API, and a
  database trigger rejects `UPDATE`/`DELETE`. Corrections are new events.
  (This is the one deliberate exception to "every entity has `updated_at`".)
* Sequence numbers per incident (`incidents.last_event_sequence`, unique `(incident_id, sequence)`)
  give a gap-free, chronological timeline independent of clock resolution.

## Consequences

* Exhaustive unit tests of the transition table; illegal transitions return
  `409 invalid_state_transition`.
* Adding a status means updating one table and its tests.
* Archival/retention will be handled by partition detach and export, never by deleting rows
  through the application.
