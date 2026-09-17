# ADR-002: Modular monolith with two entrypoints

* Status: Accepted · 2026-09-16

## Context

A small team must ship a safety-relevant MVP quickly, test it end to end, and demo it live.
Microservices would add network failure modes between the Incident Engine and the Escalation
Engine (exactly where we can least afford them), distributed transactions and operational load.

## Decision

A single Python codebase (`careos`) deployed as one image with two processes:

| Process | Responsibilities |
|---|---|
| **api** | REST, WebSocket fan-out, Device Event Gateway, SOS simulator (dev) |
| **worker** | Durable escalation execution, provider calls, device health sweep |

Bounded contexts are packages under `careos/modules/` with explicit rules:

* each module owns its models, schemas, services and router;
* routers are thin: authentication/authorisation via dependencies, then one service call;
* the Incident Engine never imports providers; the escalation executor calls providers and
  records outcomes through the Incident Engine's primitives;
* composition happens in `careos/bootstrap.py` (the only place that builds engines, Redis
  clients and providers).

## Consequences

* One transaction can span "receipt + incident + timeline + escalation timers + audit", which
  is what makes alarm ingestion atomic (ADR-004, ADR-007).
* API and worker scale independently (different replica counts, same image).
* Extraction path: modules already communicate through services and a realtime message
  contract; the gateway or notification engine can become separate deployables if load or
  compliance boundaries demand it.
