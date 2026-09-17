# ADR-005: Multi-tenancy and tenant isolation

* Status: Accepted · 2026-09-16

## Context

CareOS serves many care organisations from one deployment. Leaking one provider's service
users, incidents or audit data to another is a reportable data breach (UK GDPR Art. 33).

## Decision

Shared database, shared schema, `organisation_id` on every tenant-owned row, enforced in layers:

1. **Identity is the only source of tenant scope.** `Principal.tenant_id` comes from the
   server-side session. No endpoint accepts an organisation ID from the client for scoping.
2. **Every query filters by `principal.tenant_id`** in the service layer; lookups by ID use
   `WHERE id = :id AND organisation_id = :org`.
3. **Cross-tenant access returns 404**, not 403, so resource existence is not disclosed.
4. **Composite foreign keys** make it impossible to link, for example, an incident to another
   tenant's service user even if application code had a bug.
5. **Device gateway credentials are tenant-bound**; a device is resolved only within the
   credential's organisation.
6. **Realtime fan-out is partitioned by organisation** (Redis channel and in-process hub).
7. **Platform administrators have no tenant care-data permissions** (least privilege); tenant
   support access would be an explicit, audited, time-boxed feature.
8. **Tests:** `tests/integration/test_tenant_isolation.py` covers read, write, list, dashboard,
   audit, escalation policy, simulator, device registration and realtime isolation.

### Deferred: PostgreSQL row-level security

RLS (`SET LOCAL app.organisation_id` + policies) is valuable defence in depth. It is not in the
MVP because the worker and gateway legitimately operate across tenants and need a carefully
designed bypass role; doing that hastily would create false confidence. Planned for the first
production hardening milestone.

## Consequences

* Simple operations and cheap onboarding; one migration path for all tenants.
* Noisy-neighbour and data-residency requirements (e.g. a tenant demanding its own database)
  would need a "silo" tier later; the schema does not prevent it.
