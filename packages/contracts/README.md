# @careos/contracts

Machine-readable contracts shared between the backend, the operator console and
external integrators. **Nothing here is hand-written except `src/index.ts`.**

| Artefact | Source of truth | Consumers |
|---|---|---|
| `schemas/careos-device-event.v1.schema.json` | `careos.contracts.device_events.CareOSEvent` | device platforms, adapters, ARC integrations |
| `schemas/careos-realtime-message.v1.schema.json` | `careos.contracts.realtime.RealtimeMessage` | operator console WebSocket client |
| `openapi/careos-api.v1.json` | FastAPI application | web console, API clients |
| `src/generated/api.ts` | `openapi/careos-api.v1.json` | web console (TypeScript types) |

## Regenerating

```bash
# from apps/backend
python -m careos.cli export-contracts
# from the repository root
npm run contracts:generate
```

The backend test `test_published_json_schemas_are_up_to_date` fails if the committed
JSON Schemas drift from the Pydantic models, and CI verifies the generated TypeScript
is current.

## CareOS Device Event Contract (v1)

```json
{
  "schema_version": "1.0",
  "event_id": "evt_01J9Z3Q4W7K8M2N5P6R7S8T9V0",
  "event_type": "SOS_BUTTON",
  "device_id": "DEV-0001",
  "service_user_id": null,
  "timestamp": "2026-09-16T09:30:00Z",
  "location": { "latitude": 51.5074, "longitude": -0.1278 },
  "device": { "battery": 74, "signal": 82 },
  "metadata": {}
}
```

* `event_id` is the idempotency key. Redelivery of the identical event returns the original
  receipt; reuse with a different payload is rejected with `409 event_id_conflict`.
* `device_id` is the identifier registered in CareOS; the device registry (not the payload)
  decides which service user and organisation the event belongs to.
* `metadata` must not contain personal or health data.
