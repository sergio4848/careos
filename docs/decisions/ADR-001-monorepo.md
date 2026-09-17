# ADR-001: Monorepo layout

* Status: Accepted · 2026-09-16

## Context

CareOS ships a Python backend (API + worker), a TypeScript operator console and shared
contracts (device event schema, API types). Contract drift between them is a safety risk: a
console that misreads an incident status or a device platform that sends an unexpected
event shape can hide an alarm.

The initial brief suggested `apps/{web,api,worker}`, `packages/{contracts,config,ui}` and
`services/{incident_engine,...}` at the top level.

## Decision

One repository:

```
apps/backend       Python modular monolith (API and worker entrypoints, see ADR-002)
apps/web           Next.js operator console
packages/contracts JSON Schemas + OpenAPI + generated TypeScript types
infra/             docker init scripts, terraform placeholder
docs/              architecture, API, decisions, security, compliance
```

Deviations from the brief, deliberately:

* **No top-level `services/`.** The bounded contexts (`incident_engine`, `escalation_engine`,
  `device_gateway`, `notification_engine`, `ai_orchestrator`) live in
  `apps/backend/src/careos/modules/`. Putting Python packages outside the backend project would
  require a multi-package workspace (8+ `pyproject.toml` files, cross-package editable installs)
  for no benefit while they deploy together. See ADR-002.
* **No separate `apps/api` and `apps/worker`.** They share every domain module; they are two
  entrypoints of one image (`uvicorn careos.api.app:create_app`, `python -m careos.worker`).
* **No `packages/config` or `packages/ui` yet.** There is one frontend. They will be extracted
  when a second one (family portal, caregiver app) exists, not before.
* npm workspaces (not pnpm/turbo): no extra tooling prerequisite for contributors.

## Consequences

* Contract changes are atomic: backend model, JSON Schema, OpenAPI and TypeScript types change in
  one pull request, and CI fails on drift.
* The Python and Node toolchains stay independent (pip / npm), each with its own lockfile story.
