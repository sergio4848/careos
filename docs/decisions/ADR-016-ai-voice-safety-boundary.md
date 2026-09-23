# ADR-016: AI voice safety boundary

* Status: Accepted · 2026-09-17
* Extends: ADR-006 (AI is assistive, never safety-critical), ADR-013 (DB-enforced invariants)

## Context

Sprint 2 puts a speech model (OpenAI Realtime, configurable via `OPENAI_REALTIME_MODEL`,
mock by default) on a live call with a vulnerable person. The boundary from ADR-006 must
survive contact with a conversational model that may *say* anything.

## Decision

**What the AI may do:** greet the person by preferred name, disclose that it is automated
(the configurable greeting template and the system instructions both require this), conduct
basic wellbeing conversation, ask whether the person wants human assistance or someone
contacted, summarise the conversation, and report a structured advisory urgency signal.

**What the AI can never do — enforced in code and schema, not in prose:**

| Forbidden | Enforcement |
|---|---|
| Resolve / close / mark FALSE_ALARM | `VoiceResult` has no status field; the bridge and store never touch `Incident.status`; DB CHECK `human_only_outcomes` requires a USER actor for those statuses; `ai_cannot_change_status` forbids any AI-actor status event (migration 0002) |
| Reduce priority / cancel escalation / override operator | no code path exists; tests assert the schedule, status and priority are byte-identical across every AI outcome, including `POTENTIAL_EMERGENCY` |
| Diagnose, give treatment or medication advice, declare the person safe | `VOICE_SAFETY_INSTRUCTIONS` hard prompt; and structurally irrelevant: free text is *never parsed for decisions* — only the `report_outcome` tool call becomes data, and its schema carries no clinical fields |
| Decide emergency help is unnecessary | urgency is advisory metadata; the deterministic ladder and the operator remain authoritative |

**Structured result only.** The model must call the single `report_outcome` tool:
`{contact_established, requested_human_help, urgency_signal ∈ {NONE, ASSISTANCE_REQUESTED,
POTENTIAL_EMERGENCY}, language, summary}`. It is stored on the `ai_sessions` row and shown
to operators under the fixed label **"AI ADVISORY — HUMAN REVIEW REQUIRED"**.

**Deterministic acknowledgement is a human act.** `calls.acknowledged` becomes true only on
an explicit human action relayed through the call — `requested_human_help=true`, DTMF `1`,
or keypad `CAN_RESPOND` — mirroring Sprint 1's "answered and confirmed" semantics. The
resulting CONTACT_ACKNOWLEDGED rule (stop further *contact* attempts, operators still
verify) is the existing deterministic workflow rule, not an AI decision; an urgency signal
alone changes nothing.

## Consequences

* An AI that hallucates "the incident is resolved" can only ever produce a text summary an
  operator reads under an advisory banner; the database would reject any attempt to make it
  real.
* Tests: `test_media_stream.py` (AI failure containment), `test_voice_escalation_flow.py`
  (advisory vs. state), `test_safety_invariants.py` (DB-level), and the operator UI test
  asserting no auto-resolve control exists.
