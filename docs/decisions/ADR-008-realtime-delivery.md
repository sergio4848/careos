# ADR-008: Realtime delivery: WebSocket notifications, REST truth

* Status: Accepted · 2026-09-16

## Context

Operators must see a new SOS within a second, without refreshing. Messages can be lost
(network blips, laptop sleep, Redis restarts), and the WebSocket must not become a side channel
that bypasses authorisation.

## Decision

* **WebSocket carries notifications, not state.** A message says "incident X changed" with
  status/priority/reference only (no names, no personal data). The console refetches the
  authoritative data over REST, where authorisation is enforced.
* Messages are published **after commit** (`UnitOfWork.commit`), so consoles never see rolled-back state.
* **Fan-out:** API replicas and the worker publish to Redis pub/sub
  (`careos:realtime:org:{organisation_id}`); every API replica subscribes and delivers to its own
  sockets. If Redis is down, the API delivers to its local sockets and logs a metric.
* **Client resilience:** exponential backoff reconnect; on every (re)connect the console
  invalidates all cached queries; while disconnected it polls every 10 s and shows
  "Reconnecting · polling".
* **Security:** session cookie authentication, `Origin` allow-list (prevents cross-site WebSocket
  hijacking), organisation-partitioned hub, periodic session re-validation (revoked sessions are
  disconnected with close code 4401).

## Consequences

* Lost messages cost freshness for a few seconds, never correctness.
* Redis is not on the critical path of alarm ingestion or escalation.
* Server-Sent Events would also work; WebSocket was chosen for future bidirectional features
  (operator presence, live call control).
