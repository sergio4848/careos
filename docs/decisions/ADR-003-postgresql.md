# ADR-003: PostgreSQL as the system of record

* Status: Accepted · 2026-09-16

## Context

We need strong consistency for alarms, row-level locking for operator takeover, partial unique
indexes for invariants, JSONB for event evidence, and a mature managed offering in the UK/EU
(AWS RDS/Aurora eu-west-2, Azure Database for PostgreSQL UK South, GCP Cloud SQL europe-west2).

## Decision

PostgreSQL 18 (compose image; tests also pass on 16) via SQLAlchemy 2 async + asyncpg, schema managed by
Alembic. The database enforces invariants, not only the application:

| Invariant | Mechanism |
|---|---|
| Child rows belong to the same tenant as their parent | composite foreign keys `(organisation_id, id)` |
| One active incident per device | partial unique index on `incidents(device_id)` for active statuses |
| One active owner per incident | partial unique index on `incident_assignments(incident_id) WHERE released_at IS NULL` |
| An event is processed once | unique `(organisation_id, event_id)` on `device_event_receipts` |
| Timeline and audit are append-only | `BEFORE UPDATE OR DELETE` trigger raising an error |
| Enumerations are valid | `VARCHAR` + `CHECK` constraints (no native enums: painless migrations) |

Enums are stored as strings, UUIDv4 primary keys, `timestamptz` everywhere.

## Consequences

* Redis is optional for correctness (ADR-007, ADR-008).
* Integration tests run against real PostgreSQL; SQLite is not supported.
* Future: row-level security as defence in depth (ADR-005), time-ordered UUIDs (uuid7) when the
  runtime is Python 3.14, partitioning `incident_events`/`audit_logs` by month at scale.
