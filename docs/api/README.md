# CareOS API

* Interactive docs (development): <http://localhost:8000/docs> · ReDoc: <http://localhost:8000/redoc>
* Machine-readable: [`packages/contracts/openapi/careos-api.v1.json`](../../packages/contracts/openapi/careos-api.v1.json)
* Docs endpoints are disabled when `CAREOS_ENVIRONMENT` is `staging` or `production`.

## Conventions

* Versioned under `/v1`. JSON only. UUID identifiers. Timestamps are ISO-8601 UTC.
* Errors use one envelope and never echo submitted values:

```json
{ "error": { "code": "incident_already_assigned", "message": "…", "details": {}, "request_id": "…" } }
```

| HTTP | `code` examples |
|---|---|
| 401 | `authentication_required`, `invalid_credentials` |
| 403 | `permission_denied`, `csrf_failed` |
| 404 | `not_found` (also for other organisations' resources) |
| 409 | `invalid_state_transition`, `incident_already_assigned`, `event_id_conflict`, `conflict` |
| 413 | `payload_too_large` |
| 422 | `validation_failed` (gateway rejections include `details.reason`) |
| 429 | `rate_limited` (+ `Retry-After`) |

* Send `X-Request-ID` to correlate logs; it is echoed back.

## Authentication

### Operator console (cookie session)

```bash
curl -c jar -H 'Content-Type: application/json' \
  -d '{"email":"operator@democare.example.com","password":"<demo password>"}' \
  http://localhost:8000/v1/auth/login
# response body contains csrf_token; send it on POST/PUT/PATCH/DELETE
curl -b jar -H "X-CSRF-Token: <csrf_token>" -X POST http://localhost:8000/v1/incidents/<id>/takeover
```

### Device platforms (gateway credential)

```bash
python -m careos.cli create-gateway-key --organisation-slug demo-care-uk --name "Acme ARC"
curl -H 'X-CareOS-Gateway-Key: cgk_xxxxxxxx_…' -H 'Content-Type: application/json' \
  -d @event.json http://localhost:8000/v1/gateway/careos/events
```

## Endpoints

| Method | Path | Permission |
|---|---|---|
| POST | `/v1/auth/login` | public (rate limited) |
| GET | `/v1/auth/me` | session |
| POST | `/v1/auth/logout` | session |
| GET/POST | `/v1/users` | `users:read` / `users:manage` |
| PATCH | `/v1/users/{id}/role` | `users:manage` |
| GET | `/v1/organisations/current` | `organisation:read` |
| GET/POST | `/v1/platform/organisations` | `platform:organisations:manage` |
| GET/POST | `/v1/service-users` | `service_users:read` / `service_users:manage` |
| GET/PUT | `/v1/service-users/{id}` | `service_users:read` / `service_users:manage` |
| POST/PUT | `/v1/service-users/{id}/contacts[/{contact_id}]` | `service_users:manage` |
| GET/POST | `/v1/devices` | `devices:read` / `devices:manage` |
| POST | `/v1/gateway/{adapter}/events` | gateway credential |
| POST | `/v1/simulator/devices/{id}/events` | `simulator:use` (dev only) |
| GET | `/v1/incidents?scope=active\|awaiting_closure\|recent` | `incidents:read` |
| GET | `/v1/incidents/{id}` | `incidents:read` (audited) |
| GET | `/v1/incidents/{id}/timeline` | `incidents:read` |
| POST | `/v1/incidents/{id}/takeover` | `incidents:takeover` |
| POST | `/v1/incidents/{id}/resolve` | `incidents:resolve` |
| POST | `/v1/incidents/{id}/close` | `incidents:close` |
| GET/POST/PUT | `/v1/escalation-policies[/{id}]` | `escalation_policies:read` / `:manage` |
| GET | `/v1/dashboard/summary` | `dashboard:read` |
| GET | `/v1/audit-logs` | `audit:read` |
| WS | `/v1/ws` | session + allowed `Origin` |
| GET | `/health`, `/ready`, `/metrics` | public (restrict `/metrics` at the ingress) |

## Contracts

* Device events: [CareOS Device Event Contract](../../packages/contracts/README.md)
* Realtime: `packages/contracts/schemas/careos-realtime-message.v1.schema.json`
