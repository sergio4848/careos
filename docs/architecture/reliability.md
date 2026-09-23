# Reliability, degradation and operations

How CareOS behaves when its dependencies fail, what `/health` and `/ready` mean, and which
signals operators and on-call engineers should watch. Design rationale: ADR-007, ADR-008,
ADR-012, ADR-013.

## Dependency classification

| Dependency | Class | If unavailable |
|---|---|---|
| PostgreSQL | **Required** (system of record) | Requests fail cleanly (no partial writes); `/ready` → 503 |
| Schema at the expected revision | **Required** | `/ready` → 503 with `migrations: outdated` |
| Worker process | **Required for automation**, not for the API | Escalation steps wait durably; `/ready` stays 200 with `escalation_worker: lagging`; dashboard banner tells operators to act manually |
| Redis | **Optional** (degraded mode) | Rate limits become per-process; realtime reaches only sockets on the publishing API instance; consoles reconcile over REST (≤10 s). `/ready` stays 200 with `redis: unavailable`, `realtime: degraded` |
| Voice / SMS provider | Optional per step | Failure recorded, retried with backoff, operators alerted when exhausted |
| Twilio (real telephony) | Optional, off by default | API timeout/outage → provider failure path (retry → fail-safe operator alert); webhook loss → the call ends at its bounded maximum and is recorded TIMED_OUT; `/ready` reports `telephony` state |
| AI voice provider (OpenAI Realtime) | Optional, advisory only | Connect failure, hang, malformed events or disconnect → AI session FAILED/TIMED_OUT, `AI_CALL_FAILED` on the timeline; the phone call and the deterministic ladder continue; `/ready` reports `ai_voice` and its outage never affects SOS ingestion |
| AI provider (check-in notes) | Optional, advisory only | Timeout/failure recorded as `AI_CALL_FAILED`; deterministic flow unaffected |

Why Redis does not fail readiness: removing an API instance from the load balancer because Redis
is down would take away the only path for new alarms while fixing nothing. Every safety path
(gateway → PostgreSQL → worker) works without Redis.

Why a lagging worker does not fail API readiness: restarting or removing API instances cannot
fix the worker, and operators still need the console to handle incidents manually. Alert on it
instead.

## Endpoints

* `GET /health` — liveness. `200 {"status":"ok"}` whenever the process can serve HTTP. Use for
  container restart policies. Never touches dependencies.
* `GET /ready` — readiness. `200` only when PostgreSQL answers within 3 s **and** the schema
  revision is `current` (or `ahead` during a rolling deploy with expand/contract migrations);
  otherwise `503`. Body:

  ```json
  {
    "status": "ready",
    "database": "ok",
    "migrations": "current",
    "expected_schema_revision": "0003",
    "redis": "ok | unavailable | not_configured",
    "realtime": "ok | degraded | local_only",
    "escalation_worker": "ok | lagging | unknown",
    "telephony": "mock | disabled | twilio_simulated | twilio_live",
    "ai_voice": "mock | disabled | openai_realtime"
  }
  ```

  The Redis probe also closes the circuit breaker when Redis is back, so recovery is detected
  even without traffic.
* `GET /metrics` — Prometheus.

## Metrics to alert on

| Metric | Meaning | Suggested alert |
|---|---|---|
| `careos_sos_received_total{adapter}` | authenticated, well-formed SOS events (incl. duplicates) | drop to zero during expected traffic |
| `careos_incidents_created_total{trigger_type,priority}` | committed incidents | — |
| `careos_gateway_duplicate_events_total` | redeliveries answered from the ledger | sustained spike (device/ARC retry storm) |
| `careos_gateway_rejections_total{reason}` | `invalid_credential`, `rate_limited`, `invalid_payload`, `unknown_device`, `service_user_mismatch`, `event_id_conflict`, `unknown_adapter` | any `unknown_device` or `event_id_conflict`; spikes in `invalid_credential` |
| `careos_incident_takeovers_total{result}` | `assigned`, `conflict`, `already_owner`, `invalid_state` | — |
| `careos_incident_resolutions_total{category}` | committed resolutions | — |
| `careos_provider_failures_total{provider_kind,provider,category}` | `timeout`, `provider_error`, `unexpected` | rate > 0 for voice/notification |
| `careos_ai_timeouts_total{provider}` | AI calls abandoned | informational |
| `careos_escalation_failures_total{action_type,category}` | `retrying`, `exhausted`, `record_failed` | any `exhausted` or `record_failed` |
| `careos_realtime_delivery_failures_total{stage}` | `broker_publish`, `broker_unavailable`, `after_commit`, `socket_send` | sustained `broker_*` (Redis down) |
| `careos_calls_started_total` / `_answered_total` / `_failed_total{category}` | outbound voice calls | failed rate > 0 sustained |
| `careos_call_duration_seconds` | completed call durations | — |
| `careos_media_sessions_total{result}` | media streams (`accepted`, `rejected_auth`, `rejected_limit`, `closed_malformed`) | any `rejected_auth` spike (probing) |
| `careos_ai_voice_sessions_total{provider,result}` / `careos_ai_voice_failures_total` | in-call AI sessions | failure rate (degraded assistant) |
| `careos_ai_voice_latency_seconds` | stream start → first AI audio | p95 > 3 s |
| `careos_twilio_webhook_rejections_total{reason}` | unsigned/mis-signed/mismatched callbacks | any `bad_signature` (forgery attempts) |
| `careos_provider_idempotency_conflicts_total{kind}` | duplicate dials refused | informational (crash-window recoveries) |
| `/ready` `escalation_worker: lagging` | a step is overdue by > `CAREOS_ESCALATION_OVERDUE_AFTER_SECONDS` (60 s) | page on-call |

## Logs

JSON lines with `request_id` (from `X-Request-ID` or generated) on every request-scoped entry.
Safety events carry the identifiers needed to trace an incident across API and worker:

| Event | Fields |
|---|---|
| `gateway.event_accepted` | `adapter`, `event_id`, `event_type`, `organisation_id`, `incident_id`, `outcome` |
| `gateway.rejected` / `gateway.duplicate_event` | `adapter`, `failure_category`, `event_id`, `organisation_id` |
| `incident.created` / `.taken_over` / `.resolved` / `.closed` | `incident_id`, `organisation_id`, `event_id` or `user_id` |
| `escalation.provider_failed` | `provider_kind`, `provider`, `failure_category`, `incident_id`, `organisation_id`, `action_id`, `attempt` |
| `escalation.action_error` / `.failure_not_recorded` | `incident_id`, `organisation_id`, `action_id` |
| `ai.check_in_failed` / `ai_voice.connect_failed` | `provider`, `failure_category`, `incident_id`, `organisation_id`, `call_id` |
| `telephony.call_requested` / `.status_callback` / `.gather_response` | `provider`, `provider_call_sid`, `call_id`, `incident_id`, `organisation_id`, `result` |
| `telephony.webhook_rejected` / `.media_bridge_error` | `failure_category` (+ call/incident/organisation ids where known) |
| `realtime.publish_failed` / `.after_commit_publish_failed` | `failure_category`, `message_type`, `organisation_id`, `incident_id` |

Never logged: passwords, session tokens, CSRF tokens, gateway keys, cookies; service-user names,
phone numbers, addresses, locations and resolution notes. Enforced by key-based redaction,
`hide_parameters=True` on the database engine, and scrubbing of PostgreSQL `DETAIL`/`HINT` lines
(which can contain row values) from logged exceptions. Tested in
`tests/integration/test_observability.py`.

## Console behaviour under failure

| Condition | What the operator sees | What the console does |
|---|---|---|
| WebSocket dropped | "Live updates interrupted … Reconnecting in N s" + **Reconnect now** | Exponential backoff with jitter (1 s → 30 s cap); polls REST every 10 s |
| Reconnecting | "Reconnecting to live updates…" | On open: refetches all queries from REST |
| Silent dead connection | (as above, after ≤ 80 s) | Watchdog closes a socket with no traffic for 60 s |
| Browser back online | — | Reconnects immediately |
| API unreachable (network, 502/503/504) | Red alert "CareOS server unreachable" | Keeps retrying through normal query refetches |
| Data older than 30 s | "Data may be out of date · last updated …" | — |
| Escalation overdue | Red alert "Automated escalation is delayed" with count | — |
| Session revoked | Redirect to sign-in | Stops reconnecting |

Even while live, the console reconciles with the API every 30 s: the WebSocket is a
notification channel, PostgreSQL via the API is the source of truth.

## Recovery runbook (short)

* **Redis down:** no action needed for safety. Restore Redis; the circuit closes on the next
  successful call or `/ready` probe.
* **Worker down:** restart it. Overdue steps run on start, operator alerts first, contact steps
  in policy order. Meanwhile operators handle open incidents manually (dashboard banner).
* **Provider outage:** operators are alerted automatically after retries are exhausted; watch
  `careos_provider_failures_total`.
* **Migration pending:** `/ready` is 503 with `migrations: outdated`; run `alembic upgrade head`.
