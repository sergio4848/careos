# ADR-013: Database-enforced incident invariants

* Status: Accepted · 2026-09-17
* Related: ADR-005 (multi-tenancy), ADR-010 (state machine and timeline)

## Context

Sprint 1 enforced most incident safety rules in Python: the state machine, the model-level
status guard, "every status change has a timeline event", human-only outcomes and several
tenant references. Python guards protect the application, not the data: a bug in a future
service, a data-fix script or a raw SQL statement could bypass them. The review asked which
invariants the database should own, without over-engineering.

## Decision

Migration `0002_incident_invariants` adds constraints only where they protect a safety or
tenancy invariant that would otherwise depend on application code alone.

**Tenant-safe foreign keys** (composite `(organisation_id, id)` references, so a row can never
point into another organisation): incidents → resolved-by / closed-by users, escalation policy,
source receipt; incident events → actor user; device event receipts → device, incident;
calls → trusted contact, service user. (Incidents → service user, device, assigned user; events,
assignments, scheduled actions, AI sessions, notifications → incident were already composite.)

**Uniqueness:**

| Invariant | Constraint |
|---|---|
| Duplicate `event_id` cannot create a second receipt (per organisation) | `uq_device_event_receipts_org_event` (0001) |
| One device event opens at most one incident | `uq_incidents_source_receipt_id` (partial, new) |
| One active incident per device | `uq_incidents_one_active_per_device` (0001) |
| One active assignment per incident | `uq_incident_assignments_one_active` (0001) |
| Timeline sequence unique per incident | `uq_incident_events_incident_sequence` (0001) + `sequence > 0` |

**CHECK constraints:**

* `ck_incident_events_human_only_outcomes` — `IN_PROGRESS`, `RESOLVED`, `FALSE_ALARM`,
  `CANCELLED`, `CLOSED` can only be recorded by a `USER` actor. Providers, automation and devices
  can acknowledge and escalate, never resolve or close.
* `ck_incident_events_ai_cannot_change_status` — AI events never carry a status change.
* `ck_incident_events_user_actor_identified` — user actions always name the user.
* `ck_incident_events_from_status_requires_to_status`.
* `ck_incidents_resolution_recorded` / `ck_incidents_closure_recorded` — resolved/false-alarm
  incidents have resolver, time and category; closed incidents have closer and time.
* `ck_incident_assignments_released_after_assigned`.
* `ck_device_event_receipts_outcome_has_device` / `..._incident_outcome_linked` — an accepted
  receipt names its device; incident outcomes name their incident.
* `ck_scheduled_actions_attempts_valid` / `..._running_has_lease`.

**Status timeline triggers (deferred to COMMIT):**

* `incident_status_transitions` holds a snapshot of the state machine. A test fails if it
  differs from `state_machine.TRANSITIONS`.
* `incident_events_status_chain` and `incidents_status_recorded` are `DEFERRABLE INITIALLY
  DEFERRED` constraint triggers calling `careos_assert_incident_timeline(incident_id)`, which
  checks that status events form a legal chain starting at `RECEIVED` and that
  `incidents.status` equals the last status recorded in the timeline.

They are deferred because the engine legitimately inserts the incident row before its events
within one transaction. Consequences: `CLOSED → OPEN` is impossible even with raw SQL; a status
change without a timeline event cannot be committed; a forged event for an illegal transition
cannot be committed.

**Not added** (judged not worth the complexity now): PostgreSQL row-level security (tenancy is
enforced by composite FKs plus principal-scoped queries; RLS remains on the roadmap), a trigger
keeping `incidents.assigned_user_id` equal to the active assignment, and
`scheduled_actions.escalation_step_id` as a composite FK (it is an informational reference with
`ON DELETE SET NULL`; the executed snapshot lives in the row itself).

## Consequences

* Violations surface as `IntegrityError` (SQLSTATE 23xxx) at flush or commit; tests in
  `tests/integration/test_safety_invariants.py` bypass every Python guard to prove the database
  alone refuses the unsafe state.
* Changing the state machine now requires a migration that updates
  `incident_status_transitions` (the drift test enforces this).
* The timeline check reads an incident's status events at commit (a handful of rows, indexed by
  `(incident_id, sequence)`); negligible at current volumes.
* `/ready` reports `not_ready` if the database schema is older than the code (`migrations:
  outdated`), because these constraints would be missing.
