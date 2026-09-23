"""Real telephony (Twilio) behind the vendor-neutral VoiceProvider boundary (ADR-014).

Everything Twilio-specific lives in this package: the REST client, TwiML, webhook
signature verification, status mapping and the media-stream bridge. The Incident Engine,
Escalation Executor and state machine never import from here.
"""
