# ADR-018: Voice privacy and data retention

* Status: Accepted · 2026-09-17
* Related: ADR-016 (AI boundary), docs/security, docs/compliance

## Context

Voice calls with vulnerable people are the most sensitive data CareOS touches. Sprint 2
must add real calls without creating a store of recordings, transcripts or phone numbers.

## Decision

| Data | Policy |
|---|---|
| Raw audio | **Never stored.** Frames pass through the media bridge in memory only; `call_events` explicitly never contains audio |
| Call recording | **Off, not implemented.** No recording parameters are sent to Twilio |
| Transcripts | **Off by default.** Input transcription is not enabled on the AI session; no transcript column exists |
| Advisory summary | The only conversational residue: the model's ≤500-character structured summary, stored on `ai_sessions`, always displayed as AI advisory |
| Telephone numbers | Never persisted on call rows — only masked forms (`+44*******123`); full numbers live on the existing service-user/contact records and in provider requests only |
| AI context | Purpose-limited (`VoiceSessionContext`): preferred name, language, trigger type, approved greeting. **Not** sent: DOB, address, medical records, family history, notes, or the ServiceUser record |
| Trusted-contact script | Minimum necessary: "an active safety alert for {name}" — no condition, address or details |
| Logs | Permitted identifiers only (org/incident/call/action ids, provider, SID, correlation id, failure category); the existing redaction pipeline plus `hide_parameters` keep numbers, tokens and names out (tested) |
| Metrics | No phone number, person name or incident id ever appears as a Prometheus label |

Media-stream tokens are stored as SHA-256 digests; Twilio credentials and the OpenAI key
exist only as environment secrets (`.env.example` ships placeholders).

## Consequences

* Incident review relies on the structured advisory + the deterministic event ledger, not
  on replaying audio. If regulation ever demands recording, it must arrive as a separate,
  consent-aware ADR — this sprint deliberately did not implement it.
* Tests: `test_observability.py` (no PII/secrets in logs), `test_twilio_provider.py` (no
  full numbers in TwiML/masked fields), media tests (no audio persisted).
