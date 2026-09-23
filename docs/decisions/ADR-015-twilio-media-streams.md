# ADR-015: Twilio Media Streams and media authentication

* Status: Accepted · 2026-09-17
* Related: ADR-014 (provider boundary), ADR-016 (AI voice safety), ADR-018 (privacy)

## Context

The AI safety assistant talks to the service user inside the phone call. Twilio's
bidirectional Media Streams (`<Connect><Stream>`) deliver G.711 μ-law 8 kHz audio over a
WebSocket that CareOS must expose publicly — which must never become an unauthenticated
side door into tenant data.

## Decision

* **Dedicated endpoint** `wss://…/v1/providers/twilio/media`, completely separate from the
  operator dashboard WebSocket (different router, no session cookies, no hub).
* **Single-use opaque token.** The executor mints a random token per welfare call, stores
  only its SHA-256 digest on the call row, and places the raw token in the TwiML as a
  `<Parameter>` — it arrives inside the `start` frame, never in a URL (so never in access
  logs). The token maps server-side to exactly one call → incident → organisation; a client
  can never supply those identifiers. First use consumes it; replays, unknown tokens,
  terminal calls and pre-`start` traffic close the socket (policy violation) without
  explanation.
* **Protocol handling.** `connected`, `start`, `media`, `dtmf`, `mark`, `stop` are handled;
  unknown events are ignored (Twilio adds new ones). Outbound audio is sent as `media` +
  `mark`; on the AI provider's `speech_started` the bridge sends `clear` so queued AI audio
  stops immediately (barge-in).
* **Bounds.** Per-message size limit (`CAREOS_MEDIA_MAX_MESSAGE_BYTES`), process-wide
  concurrent-stream cap (`CAREOS_MEDIA_MAX_CONNECTIONS`), 10 s handshake timeout and the
  overall `CAREOS_VOICE_CALL_MAX_DURATION_SECONDS`. Malformed frames end the session safely
  (AI session FAILED, escalation untouched).
* **Bridge abstraction.** `TelephonyMediaBridge` connects Twilio frames to the
  `AIVoiceProvider` session; codecs/protocol never reach the Incident Engine. Finalisation
  (persisting the advisory, timeline entries, metrics) is shielded from task cancellation, so
  an abrupt transport teardown still records the outcome exactly once.

## Consequences

* Requires a public HTTPS/WSS host in real deployments (documented in the README); local
  mock development needs none of it.
* Audio passes through CareOS memory only; nothing audio-shaped is persisted (ADR-018).
* Tests: `tests/integration/test_media_stream.py` (auth, replay, malformed, oversized,
  disconnect, DTMF, barge-in, AI failures).
