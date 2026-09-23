"""TwiML documents CareOS hands to Twilio. Minimum necessary information only (ADR-018)."""

from __future__ import annotations

import uuid
from xml.sax.saxutils import escape, quoteattr


def status_callback_url(base_url: str, call_id: uuid.UUID) -> str:
    return f"{base_url.rstrip('/')}/v1/providers/twilio/voice/status?call={call_id}"


def gather_action_url(base_url: str, call_id: uuid.UUID) -> str:
    return f"{base_url.rstrip('/')}/v1/providers/twilio/voice/gather?call={call_id}"


def media_stream_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    wss = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return f"{wss}/v1/providers/twilio/media"


def service_user_stream_twiml(base_url: str, media_token: str) -> str:
    """Bidirectional media stream: the AI safety assistant talks to the service user.

    The single-use token travels as a <Parameter> (delivered in the ``start`` frame), not
    in the URL, so it never appears in HTTP access logs.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f"<Stream url={quoteattr(media_stream_url(base_url))}>"
        f'<Parameter name="token" value={quoteattr(media_token)}/>'
        "</Stream>"
        "</Connect></Response>"
    )


def trusted_contact_gather_twiml(
    base_url: str, call_id: uuid.UUID, subject_name: str, language: str = "en-GB"
) -> str:
    """Deterministic trusted-contact flow: minimum necessary information, keypad response."""
    say = escape(
        f"This is CareOS, the automated safety service, regarding an active safety alert "
        f"for {subject_name}. "
        "Press 1 if you can respond. Press 2 if you cannot respond right now. "
        "Press 3 to ask a CareOS operator to handle it."
    )
    lang = quoteattr(language)
    action = quoteattr(gather_action_url(base_url, call_id))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Gather numDigits="1" timeout="10" action={action} method="POST">'
        f"<Say language={lang}>{say}</Say>"
        "</Gather>"
        f"<Say language={lang}>No input received. Goodbye.</Say>"
        "</Response>"
    )


def gather_ack_twiml(language: str = "en-GB") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say language={quoteattr(language)}>"
        "Thank you. Your response has been recorded.</Say></Response>"
    )
