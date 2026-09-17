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

## Update · 2026-09-17 (hardening review, see ADR-012)

* **Bounded, contained publishing.** After-commit publishing can no longer fail or slow a
  committed request: every exception is caught and counted
  (`careos_realtime_delivery_failures_total{stage}`), and each message is bounded by
  `CAREOS_REALTIME_PUBLISH_TIMEOUT_SECONDS` (1 s). Previously an unexpected broker error returned
  HTTP 500 for an SOS that had already been stored, and an unreachable Redis added ~2 s per message.
* **Redis circuit breaker.** Redis timeouts are 0.5 s; after a failure the broker and rate limiter
  skip Redis for 5 s (local delivery / per-process limits) and then probe once. `/ready` exposes
  `redis` and `realtime` state and does not fail readiness for Redis.
* **Client state machine.** `RealtimeConnection` (framework-independent, unit tested):
  `connecting → live → disconnected → reconnecting → live`, terminal `unauthorised`. Bounded
  exponential backoff with jitter (1 s → 30 s), immediate retry on the browser `online` event or
  the operator's "Reconnect now", and a watchdog that treats 60 s without traffic as a dead socket.
* **Reconciliation while live.** Queries refetch from REST every 30 s even when live (a lost
  notification is corrected within 30 s), every 10 s when not live, and all queries refetch on
  every (re)connect.
* **Operator-visible degradation.** Banners for API unreachable, live updates interrupted /
  reconnecting, stale data (> 30 s) and delayed escalation.

