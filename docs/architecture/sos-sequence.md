# SOS end-to-end sequence

```mermaid
sequenceDiagram
  autonumber
  actor Op as Operator (browser)
  participant Web as Next.js console
  participant API as API (FastAPI)
  participant GW as Device Gateway
  participant IE as Incident Engine
  participant PG as PostgreSQL
  participant R as Redis pub/sub
  participant W as Worker
  participant V as VoiceProvider (mock)

  Op->>Web: SEND TEST SOS (Simulator page)
  Web->>API: POST /v1/simulator/devices/{id}/events (session + CSRF)
  API->>GW: POST /v1/gateway/simulator/events (vendor payload, gateway key, X-Request-ID)
  GW->>GW: authenticate credential → organisation; adapter → CareOS Event
  GW->>PG: BEGIN; INSERT receipt ON CONFLICT DO NOTHING
  GW->>PG: SELECT device FOR UPDATE; upsert telemetry
  GW->>IE: handle_device_event
  IE->>PG: INSERT incident (CRITICAL) + events SOS_RECEIVED…INCIDENT_OPENED
  IE->>PG: INSERT scheduled_actions (policy snapshot) + ESCALATION_SCHEDULED + audit
  GW->>PG: COMMIT
  GW-->>R: publish incident.created (after commit)
  R-->>API: subscriber delivers to org's sockets
  API-->>Web: WS {type: incident.created}
  Web->>API: GET /v1/incidents, /v1/dashboard/summary
  Web-->>Op: red CRITICAL card, live timer

  loop every second
    W->>PG: claim due actions (FOR UPDATE SKIP LOCKED, lease)
  end
  W->>PG: T+0 prepare: OPEN → CONTACTING, AUTOMATED_CALL_STARTED
  W->>V: place_call (timeout)
  V-->>W: NO_ANSWER
  W->>PG: AUTOMATED_CALL_NO_ANSWER, step COMPLETED
  W-->>R: incident.updated
  Note over W,V: T+30 contact #1, T+60 contact #2, T+90 OPERATOR_ESCALATION → ESCALATED

  Op->>Web: TAKE OVER
  Web->>API: POST /v1/incidents/{id}/takeover
  API->>PG: SELECT incident FOR UPDATE; INSERT assignment; → IN_PROGRESS; cancel pending steps; audit
  API-->>R: incident.updated
  Op->>Web: RESOLVE (category, notes)
  Web->>API: POST /v1/incidents/{id}/resolve
  API->>PG: → RESOLVED, INCIDENT_RESOLVED event, audit
  Op->>Web: CLOSE
  API->>PG: → CLOSED, assignment released, audit
```
