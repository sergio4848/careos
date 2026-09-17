# ADR-006: AI is assistive, never safety-critical

* Status: Accepted · 2026-09-16

## Context

Voice AI and LLMs can make welfare check-ins more natural and summarise long incidents. They
are also non-deterministic, can be unavailable, slow or wrong. CareOS must not diagnose,
predict illness, recommend treatment or let AI independently decide on an emergency.

## Decision

* The Incident Engine and escalation state transitions are deterministic code. **No AI output
  can change incident status, priority, assignment, escalation or resolution.** There is no API
  for it.
* AI runs only inside the escalation executor, behind `AIOrchestrator`, which:
  * enforces a timeout on every call,
  * catches every exception and returns an `AIOutcome` instead of raising,
  * records an `AISession` and a timeline event (`AI_CALL_STARTED/COMPLETED/FAILED`).
* The deterministic path always continues: the automated welfare call is placed through the
  `VoiceProvider` whether AI succeeded, failed or is disabled. AI assistance runs **concurrently**
  with the call, so a slow or hanging AI provider cannot delay it
  (`test_slow_ai_never_delays_the_deterministic_call`, `test_escalation_runs_with_ai_disabled`).
* AI text is stored as `advisory_summary`, rendered in the console inside a clearly labelled
  "AI-generated · advisory only" box.
* Providers are selected by configuration (`CAREOS_AI_PROVIDER=mock|mock_unavailable|disabled`);
  `mock_unavailable` exists to demonstrate an outage live.

## Consequences

* An AI outage degrades the experience, never safety (`test_ai_outage_does_not_affect_incident_handling`).
* Any future AI feature that wants to influence the workflow needs a new ADR, a clinical-safety
  review (DCB0129/DCB0160 in the UK) and human confirmation in the loop.
