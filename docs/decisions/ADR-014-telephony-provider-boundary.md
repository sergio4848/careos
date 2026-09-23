# ADR-014: Telephony provider boundary and real-call development safety

* Status: Accepted · 2026-09-17
* Related: ADR-007 (durable escalation), ADR-012 (transaction boundaries), ADR-017 (idempotency)

## Context

Sprint 2 adds real outbound telephone calls (Twilio) to the escalation flow. The Incident
Engine and state machine must stay vendor-free, calls must remain at-least-once safe under
worker crashes, and a developer laptop must never accidentally ring a real person.

## Decision

**Boundary.** `TwilioVoiceProvider` implements the existing `VoiceProvider` protocol behind
`careos.modules.telephony`; nothing outside that package imports Twilio concepts. The chain
stays `ScheduledAction → EscalationExecutor → VoiceProvider`; `MockVoiceProvider` remains the
default and the only provider used in CI.

**Blocking contract kept.** `place_call` still returns a final `VoiceCallResult`. The Twilio
implementation starts the call (REST API, inline TwiML), then waits on PostgreSQL call state,
which the signed status webhooks and the media bridge populate. State machine authority never
moves: only the executor performs incident status transitions; webhooks and media events
write call/AI records only. Providers may additionally declare
`operation_deadline_seconds` (executor timeout for one call) and `cancel_call(sid)`
(operator STOP hangup); the executor discovers both by attribute, keeping the protocol small.

**Twilio's vocabulary never leaks.** Callback statuses are mapped once
(`telephony/states.py`) onto the internal lifecycle QUEUED → INITIATED → RINGING → ANSWERED →
IN_PROGRESS → {COMPLETED, NO_ANSWER, BUSY, FAILED, CANCELLED, TIMED_OUT}, forward-only.

**Real-call safety switches**, all enforced in `TwilioVoiceProvider` before any network call:

1. `CAREOS_REAL_TELEPHONY_ENABLED=false` by default — the provider refuses every call.
2. Outside production the destination must be in `CAREOS_TELEPHONY_ALLOWED_NUMBERS`
   (configuration refuses to enable real telephony without a non-empty allowlist).
3. Destinations must be valid E.164; enabling real telephony requires SID, auth token,
   From number and a public **https** `CAREOS_TWILIO_WEBHOOK_BASE_URL` at boot.

A refusal surfaces as a provider failure: retries, then the deterministic fail-safe operator
alert — never a silent skip.

**UK-first region.** `TWILIO_REGION` / `TWILIO_EDGE` are configuration (defaults `ie1` /
`dublin`, Twilio's Ireland region) and build the API host; no US region is hard-coded.
Limitations: Twilio feature availability differs by region — media-stream traffic follows the
configured edge, some Twilio products are global-region only, and an account must have the
IE1 region enabled; setting the variables empty falls back to Twilio's global (US) region.

## Consequences

* CI and local development run entirely on mocks; `docker compose up` needs no cloud account.
* One deliberate cost: `place_call` holds a worker slot for the duration of a call (bounded
  by ring timeout + `CAREOS_VOICE_CALL_MAX_DURATION_SECONDS`); concurrency is the executor's
  semaphore. Acceptable at telecare call volumes; revisit before high call concurrency.
* Tests: `tests/unit/test_twilio_provider.py`, `tests/unit/test_telephony_security.py`,
  `tests/integration/test_voice_escalation_flow.py`.
