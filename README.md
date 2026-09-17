# CareOS

**Telecare & Safety Incident Orchestration Platform** (development codename).

CareOS receives alarms from SOS pendants and connected care systems, turns them into
incidents, runs an organisation's escalation policy, gives operators a real-time board and
keeps an immutable timeline and audit trail. First market: UK telecare providers, home-care
companies, assisted-living providers and alarm receiving centres.

> **Product boundary.** CareOS does not diagnose, predict illness or recommend treatment, and
> AI never makes or closes a safety decision. The Incident Engine and Escalation Engine are
> deterministic and keep working when the AI provider, Redis or a notification provider is down.

## Contents

[Architecture](#architecture) · [Requirements](#requirements) · [Installation](#installation) ·
[Environment](#environment) · [Database migrations](#database-migrations) · [Seed data](#seed-data) ·
[Run](#run) · [Tests](#tests) · [API docs](#api-docs) · [Demo](#demo) ·
[Known limitations](#known-limitations)

---

## Architecture

```
SOS Simulator (dev page)          device platform / ARC (future)
        │ vendor payload + gateway key   │
        ▼                                ▼
┌──────────────────────────── API (FastAPI) ─────────────────────────────┐
│ Device Gateway → adapter → CareOS Event Contract → idempotency receipt │
│      │                                                   (one DB tx)   │
│      ▼                                                                 │
│ Incident Engine: state machine · IncidentEvent timeline · audit ·      │
│                  escalation timers                                     │
│ REST (session cookie + CSRF, RBAC, tenant scope) · WebSocket hub       │
└──────┬───────────────────────────────────────────────▲─────────────────┘
       │ PostgreSQL (system of record)                 │ Redis pub/sub (fan-out only)
┌──────▼───────────────────────────────────────────────┴─────────────────┐
│ Worker: claims due escalation steps (FOR UPDATE SKIP LOCKED)           │
│         → mock VoiceProvider / NotificationProvider / AIProvider       │
└────────────────────────────────────────────────────────────────────────┘
       ▲ REST + WebSocket
Next.js operator console: /dashboard · /incidents/{id} · /dev/sos-simulator · /audit
```

* **Modular monolith**: one Python codebase (`apps/backend`), two processes: `api` and `worker`.
* **Incident Engine** (`modules/incident_engine`) owns the state machine; routes never change status.
* **Append-only timeline**: `incident_events` and `audit_logs` reject `UPDATE`/`DELETE` via a DB trigger.
* **Escalation** timers are rows in PostgreSQL, created in the same transaction as the incident.
* **Tenant isolation** is enforced from the authenticated principal and by composite foreign keys.
* Decisions and trade-offs: [`docs/decisions`](docs/decisions/README.md) · overview: [`docs/architecture`](docs/architecture/overview.md)

| Layer | Stack |
|---|---|
| Web | Next.js 16, React 19, TypeScript, TanStack Query, CSS Modules, Vitest |
| Backend | Python 3.13, FastAPI, Pydantic 2, SQLAlchemy 2 (async), Alembic, structlog |
| Data | PostgreSQL 18, Redis 8 |
| Tooling | Docker Compose, pytest, ruff, mypy, ESLint, GitHub Actions |

## Requirements

* Docker Engine with Compose v2 (Docker Desktop on macOS/Windows; Windows needs WSL 2 enabled).
* Free ports on `127.0.0.1`: `3000`, `8000`, `5432`, `6379`.
* Only for development without Docker: Python 3.13, Node.js 24 / npm 11, PostgreSQL 16+.

## Installation

```bash
git clone <repository-url> careos
cd careos
cp .env.example .env        # optional: compose has development defaults
```

## Environment

All configuration uses `CAREOS_*` environment variables; every variable is documented in
[`.env.example`](.env.example). `docker-compose.yml` provides **development-only** defaults so
the stack starts without a `.env` file. Never commit `.env`.

Security-relevant settings:

| Variable | Purpose |
|---|---|
| `CAREOS_SECRET_KEY` | HMAC key for session tokens, CSRF tokens and gateway keys (≥ 32 chars) |
| `CAREOS_CORS_ALLOWED_ORIGINS` | explicit browser origins for the API and WebSocket |
| `CAREOS_COOKIE_SECURE` | `true` behind HTTPS |
| `CAREOS_SIMULATOR_ENABLED`, `CAREOS_SIMULATOR_GATEWAY_KEY` | development SOS simulator |
| `CAREOS_SEED_DEMO_PASSWORD` | password for seeded demo users |

With `CAREOS_ENVIRONMENT=staging|production` the API refuses to start if the secret key is a
placeholder, cookies are not `Secure`, the simulator or embedded worker is enabled, or CORS allows `*`.

## Database migrations

Migrations run automatically in the one-shot `migrate` service. Manually:

```bash
docker compose run --rm migrate                          # alembic upgrade head + seed
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic downgrade base
docker compose run --rm api alembic check                # fails if models and schema differ
```

## Seed data

```bash
docker compose run --rm api python -m careos.cli seed-demo   # idempotent
```

| Entity | Value |
|---|---|
| Organisation | Demo Care UK |
| Service user | Margaret Wilson, London |
| Trusted contacts | #1 Sarah Wilson (daughter), #2 James Wilson (son) |
| Device | `DEV-0001`, `SOS_PENDANT`, ONLINE, battery 84 %, signal 92 % |
| Escalation policy | T+0 `AUTOMATED_USER_CONTACT` · T+30 trusted contact #1 · T+60 trusted contact #2 · T+90 `OPERATOR_ESCALATION` |
| Users (password `CAREOS_SEED_DEMO_PASSWORD`, compose default `CareOS-Demo-2026!`) | `operator@democare.example.com`, `operator2@democare.example.com`, `manager@democare.example.com` (audit access), `admin@democare.example.com` |
| Second tenant (isolation checks) | Northshire Telecare Ltd, `operator@northshire.example.com` |

All people are fictional; phone numbers use Ofcom's ranges reserved for drama.

## Run

```bash
docker compose up --build
```

| URL | |
|---|---|
| <http://localhost:3000/dashboard> | Operator dashboard |
| <http://localhost:3000/dev/sos-simulator> | SOS Simulator (development only) |
| <http://localhost:8000/health> · <http://localhost:8000/ready> | Liveness · readiness |
| <http://localhost:8000/docs> | OpenAPI UI |

Reset all data: `docker compose down --volumes`.

<details>
<summary>Run without Docker (single process, no Redis)</summary>

```bash
cd apps/backend
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
export CAREOS_DATABASE_URL=postgresql+asyncpg://<user>:<password>@localhost:5432/careos
export CAREOS_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export CAREOS_SEED_DEMO_PASSWORD='<demo password>'
export CAREOS_SIMULATOR_GATEWAY_KEY=cgk_demo0001_<random-secret-16-chars-or-more>
export CAREOS_REDIS_URL=                  # empty: in-process realtime
export CAREOS_EMBEDDED_WORKER=true        # development only: worker loops inside the API
alembic upgrade head && python -m careos.cli seed-demo
uvicorn careos.api.app:create_app --factory --port 8000

# second terminal, repository root
npm ci
CAREOS_PUBLIC_API_URL=http://localhost:8000 npm run dev:web
```
</details>

## Tests

Backend tests run against a real PostgreSQL database (`CAREOS_TEST_DATABASE_URL`; its schema is
dropped and re-migrated at the start of each run, so point it at a dedicated database). Set
`CAREOS_TEST_REDIS_URL` to also run the real-Redis tests (skipped otherwise; CI runs them).

```bash
# Backend (apps/backend) — the same steps CI runs
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
alembic upgrade head && alembic downgrade base && alembic upgrade head && alembic check
pytest tests/unit
export CAREOS_TEST_DATABASE_URL=postgresql+asyncpg://careos:careos@localhost:5432/careos_test
export CAREOS_TEST_REDIS_URL=redis://localhost:6379/15   # optional
pytest tests/integration

# Contracts are generated, never edited (repository root)
(cd apps/backend && python -m careos.cli export-contracts) && npm run contracts:generate
git diff --exit-code -- packages/contracts

# Web: lint, types, component tests, production build (repository root)
npm ci && npm run lint && npm run typecheck && npm run test && npm run build:web

# Everything inside Docker
docker compose run --rm api pytest
```

Reliability suites (technical review hardening):

| Suite | Proves |
|---|---|
| `tests/integration/test_failure_injection.py` | SOS persisted and escalated with Redis unreachable; broker raising/hanging never fails or delays a committed SOS; AI hanging/raising never blocks or changes an incident; voice (error, SDK exception, hang) and SMS failures are recorded and escalate to humans; worker crash + 10 min downtime recovers in policy order; same `event_id` ×8 concurrently → one receipt, one incident; 8 distinct SOS concurrently from one device → one active incident, gap-free timeline; crash before commit leaves nothing behind |
| `tests/integration/test_safety_invariants.py` | CLOSED cannot reopen (API, ORM, raw SQL, forged event); every status change is in the timeline (enforced at commit); assignments cannot cross tenants and only one is active; duplicate `event_id` refused by the database; organisation consistency of incidents/events/receipts; resolved ≠ closed; AI cannot change status; providers/automation cannot record human outcomes; takeover/resolution/closure audited with actor and tenant |
| `tests/integration/test_security_regression.py` | For 14 endpoints, another organisation's real identifiers get responses identical to random identifiers; lists, counts, dashboard and audit unchanged by another tenant's activity; gateway keys cannot reach other tenants' devices; WebSocket events never cross organisations |
| `tests/integration/test_observability.py` | safety metrics move exactly as expected and are exposed; logs carry request, incident, organisation, event, provider and failure category; no passwords, tokens, gateway secrets, cookies or service-user PII in logs, including database error details |
| `tests/unit/test_reliability_primitives.py` | circuit breaker; Redis fallbacks; Unit of Work after-commit semantics |
| `apps/web/src/lib/realtime/connection.test.ts` and banner/freshness tests | reconnect state machine, bounded backoff, REST refresh after reconnect, dead-socket watchdog, API-unavailable / stale-data / escalation-delay notices |

Sprint 1 acceptance tests (`apps/backend/tests/integration/test_sprint1_acceptance.py`):

| Test | Verifies |
|---|---|
| `test_sos_creates_incident` | simulator → gateway → CRITICAL OPEN incident persisted, dashboard counters |
| `test_duplicate_event_does_not_create_second_incident` | same `event_id` twice and concurrently → one incident |
| `test_invalid_device_rejected` | unknown device, malformed payload, bad gateway key rejected and audited |
| `test_cross_tenant_device_access_denied` | other organisation cannot raise, see or act on the device/incident |
| `test_invalid_state_transition` | `CLOSED → OPEN` forbidden; illegal API actions return 409 |
| `test_operator_takeover` | assignment, `OPERATOR_TAKEOVER` event, audit, push to other dashboards |
| `test_double_operator_takeover` | two concurrent takeovers → exactly one owner |
| `test_incident_resolution` | category, optional notes, resolved by/at; resolved ≠ closed |
| `test_incident_event_timeline` | ordered sequence, required events, append-only |
| `test_audit_log_created` | required audit codes, read restricted, immutable |

## Real Voice Development Setup

By default CareOS runs entirely on **mock providers**: `docker compose up` works with no
Twilio account, no OpenAI key and no public domain, and CI never contacts a real API.
Real calling is an explicit, allowlisted opt-in.

**Requirements for real calls**

| Concept | Setting |
|---|---|
| Master safety switch | `CAREOS_REAL_TELEPHONY_ENABLED=true` (default `false`: Twilio is never contacted) |
| Development allowlist | `CAREOS_TELEPHONY_ALLOWED_NUMBERS=+44…` — outside production, only these E.164 numbers can be called (required, never commit personal numbers) |
| Twilio | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_REGION` (default `ie1`), `TWILIO_EDGE` (default `dublin`) |
| Public HTTPS/WSS | `CAREOS_TWILIO_WEBHOOK_BASE_URL=https://…` — Twilio must reach `POST /v1/providers/twilio/voice/status|gather` and `wss://…/v1/providers/twilio/media` (use a tunnel such as `ngrok http 8000` or `cloudflared` in development) |
| Voice provider | `CAREOS_VOICE_PROVIDER=twilio` (default `mock`) |
| AI voice | `CAREOS_AI_VOICE_PROVIDER=openai_realtime` with `OPENAI_API_KEY`, `OPENAI_REALTIME_MODEL` (default stays `mock`: a scripted assistant, no network) |

**Test mode (no cloud accounts).** Keep every default: the mock voice provider resolves
calls instantly, the mock AI voice assistant runs the same media-bridge code path, and the
whole Sprint 2 flow is exercised by `pytest tests/integration/test_voice_escalation_flow.py`.

**Real-call demo** (after configuring the table above and `alembic upgrade head`):

1. Put *your own* phone number in `CAREOS_TELEPHONY_ALLOWED_NUMBERS` and set it as the demo
   service user's number.
2. Start the stack, open the dashboard, and press **SEND TEST SOS** in the simulator.
3. At T+0 your phone rings from `TWILIO_FROM_NUMBER`; the automated assistant introduces
   itself as automated, listens, and says it will connect your care team. Say
   *"Yes, I need someone to help me."*
4. The incident page shows the live call (CALLING → RINGING → CONNECTED → ENDED), the
   structured advisory under **AI ADVISORY — HUMAN REVIEW REQUIRED**, urgency
   `ASSISTANCE_REQUESTED`, and the incident stays active until you **Take over** and
   resolve it manually.

**Security caveats.** The webhook/media endpoints must be reachable *only* via the HTTPS
base you configure; callbacks are verified against `X-Twilio-Signature` and media streams
against single-use tokens, but do not expose a development tunnel longer than needed. Raw
audio, recordings and transcripts are never stored (ADR-018). Never commit `.env`,
credentials or personal numbers.

## API docs

* Swagger UI: <http://localhost:8000/docs> · ReDoc: <http://localhost:8000/redoc> (disabled in staging/production)
* OpenAPI file: [`packages/contracts/openapi/careos-api.v1.json`](packages/contracts/openapi/careos-api.v1.json)
* CareOS Event Contract (JSON Schema): [`packages/contracts`](packages/contracts/README.md)
* Endpoint overview and auth examples: [`docs/api`](docs/api/README.md)

## Demo

### Margaret Wilson SOS Demo

| # | Step | Expected result |
|---|---|---|
| 1 | **Start Docker:** `docker compose up --build` and wait until `web` is healthy. | `http://localhost:8000/ready` → `{"status":"ready",...}` |
| 2 | **Run the seed:** `docker compose run --rm api python -m careos.cli seed-demo` (already done by `migrate`; safe to repeat). | "Demo seed: skipped (already present)" or "created" |
| 3 | **Open the dashboard:** <http://localhost:3000/dashboard>, sign in as `operator@democare.example.com`. | Tiles: Active incidents 0, Critical 0, Unacknowledged 0, Devices online 2, Devices offline 1. Connection pill shows **LIVE**. |
| 4 | **Open the SOS Simulator** in a second window: <http://localhost:3000/dev/sos-simulator>. | Card: **Margaret Wilson · Device DEV-0001 · Battery 84% · Signal 92% · Status ONLINE** |
| 5 | Press **SEND TEST SOS**. | Result: *Gateway 202 · Incident created*. The expandable vendor payload shows the simulator sent a device message to the gateway, not an incident request. |
| 6 | **Watch the dashboard** (no refresh). | A red **CRITICAL** card appears within a second: Margaret Wilson · SOS BUTTON · Elapsed `00:01, 00:02…` · Status OPEN · Device DEV-0001. Critical and Unacknowledged turn 1. |
| 7 | Click **VIEW INCIDENT**. | Service user, device, location (London + GPS), priority, current status, assigned operator (unassigned), live elapsed time, and the timeline: `SOS_RECEIVED → INCIDENT_CREATED → VALIDATION_STARTED → INCIDENT_OPENED → ESCALATION_SCHEDULED`. Within seconds the mock escalation adds `AUTOMATED_CALL_STARTED`, `AI_CALL_STARTED/COMPLETED` (advisory), `AUTOMATED_CALL_NO_ANSWER`; at T+30 `TRUSTED_CONTACT_CALLED · Sarah Wilson`. |
| 8 | Click **TAKE OVER**. | Status **In progress**, Assigned operator **Olivia Grant**. Other open dashboards update immediately; a second operator pressing TAKE OVER is refused. |
| 9 | **Check the timeline.** | `OPERATOR_TAKEOVER (Contacting → In progress)` and `ESCALATION_HALTED` (pending mock steps cancelled). |
| 10 | Click **RESOLVE INCIDENT**, choose *Family responded*, optionally add notes, confirm. | Status **Resolved**; Resolution panel shows category, resolved by, timestamp; `INCIDENT_RESOLVED` appended. The card moves to *Resolved · awaiting closure*. |
| 11 | **Check the audit log:** sign in as `manager@democare.example.com` → **Audit trail** (or the incident page's audit panel). | `INCIDENT_CREATED`, `INCIDENT_VIEWED`, `INCIDENT_TAKEOVER`, `INCIDENT_STATE_CHANGED`, `INCIDENT_RESOLVED` with actor and time. |

Optional checks: **Resend last event** in the simulator's options returns *duplicate event,
no new incident*; signing in as `operator@northshire.example.com` shows no Demo Care UK data.

## Known limitations

* **Reliability model:** see [docs/architecture/reliability.md](docs/architecture/reliability.md).
  Provider calls are at-least-once (a worker crash between a call and recording its result can
  repeat the call). Realtime delivery is best-effort; consoles reconcile over REST within 30 s
  (10 s when live updates are down). Escalation timing is bounded by the worker poll interval
  and, after a worker crash, by the 60 s lease.
* **Redis outage with several API replicas:** consoles connected to a replica other than the one
  that accepted the SOS see it through REST reconciliation (≤10 s), not instantly.
* **Docker is not available on the development machine used so far**; the Compose stack is
  verified in the CI `docker` job (build, `--wait` health, smoke test, tests inside the image).
* **No browser automation tests.** The UI is covered by component tests (Vitest) and an
  API + WebSocket end-to-end script; there are no Playwright tests yet.
* **Real voice/AI needs opt-in configuration.** Twilio calling and the OpenAI Realtime
  assistant exist behind `CAREOS_REAL_TELEPHONY_ENABLED` + an allowlist (see
  *Real Voice Development Setup*); CI and default deployments run mocks. Live Twilio/OpenAI
  integration has not yet been exercised against the real services from this development
  machine (no public HTTPS endpoint here); the protocol layers are covered by contract-level
  fakes. SMS remains mock-only.
* **Voice concurrency**: one worker slot is held for a call's duration (bounded by
  `CAREOS_VOICE_CALL_MAX_DURATION_SECONDS`); fine for telecare volumes (ADR-014).
* **Management through the API only.** Users, service users, contacts, devices and escalation
  policies have REST endpoints but no admin screens.
* **Authentication is email + password sessions.** No MFA or SSO; no password reset flow.
* **Defence in depth still to add:** PostgreSQL row-level security, nonce-based CSP, column encryption.
* **Escalation precision** is bounded by the worker poll interval (default 1 s); calls are at-least-once.
* **Dependency locks:** Python transitive dependencies are not locked yet; ESLint is held at 9.x
  because `eslint-config-next` does not run on ESLint 10.
* `next start` warns because the app uses `output: "standalone"`; the Docker image runs
  `node apps/web/server.js` as intended.

## Licence

Not yet decided — see [LICENSE.md](LICENSE.md).
