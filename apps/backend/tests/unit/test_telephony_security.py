"""Twilio signatures, number hygiene, TwiML documents and internal state mapping."""

from __future__ import annotations

import uuid

from careos.core.config import Settings
from careos.modules.notification_engine.models import TERMINAL_CALL_STATUSES, CallStatus
from careos.modules.telephony.media import format_greeting
from careos.modules.telephony.security import (
    compute_twilio_signature,
    is_e164,
    mask_number,
    media_token_digest,
    new_media_token,
    verify_twilio_signature,
)
from careos.modules.telephony.states import map_twilio_status, may_advance
from careos.modules.telephony.twiml import (
    service_user_stream_twiml,
    status_callback_url,
    trusted_contact_gather_twiml,
)
from tests.conftest import make_settings

BASE = "https://careos.example"


def test_twilio_signature_round_trip() -> None:
    url = f"{BASE}/v1/providers/twilio/voice/status?call=abc"
    params = {"CallSid": "CA123", "CallStatus": "ringing", "SequenceNumber": "1"}
    signature = compute_twilio_signature("auth-token", url, params)
    assert verify_twilio_signature("auth-token", url, params, signature)
    assert not verify_twilio_signature("auth-token", url, params, None)
    assert not verify_twilio_signature("auth-token", url, params, signature[:-2] + "xx")
    assert not verify_twilio_signature("other-token", url, params, signature)
    assert not verify_twilio_signature("auth-token", url + "&x=1", params, signature)
    assert not verify_twilio_signature(
        "auth-token", url, {**params, "CallStatus": "completed"}, signature
    )


def test_e164_validation() -> None:
    assert is_e164("+447700900123")
    assert is_e164("+14155550100")
    for bad in ("+44 7700 900123", "07700900123", "+0447700900123", "+44", "447700900123", ""):
        assert not is_e164(bad), bad


def test_number_masking_keeps_only_edges() -> None:
    assert mask_number("+447700900123") == "+44*******123"
    assert "7700900" not in mask_number("+447700900123")
    assert mask_number("+4420") == "*****"  # too short to keep anything


def test_media_tokens_are_single_purpose_secrets() -> None:
    token = new_media_token()
    assert len(token) >= 32
    assert media_token_digest(token) != token
    assert media_token_digest(token) == media_token_digest(token)
    assert new_media_token() != token


def test_service_user_twiml_uses_wss_stream_with_token_parameter() -> None:
    token = new_media_token()
    twiml = service_user_stream_twiml(BASE, token)
    assert "<Connect>" in twiml and "<Stream" in twiml
    assert 'url="wss://careos.example/v1/providers/twilio/media"' in twiml
    assert f'<Parameter name="token" value="{token}"/>' in twiml
    # The token rides in a Parameter (start frame), never in the URL (access logs).
    assert "media?token" not in twiml


def test_trusted_contact_twiml_is_minimal_and_escaped() -> None:
    call_id = uuid.uuid4()
    twiml = trusted_contact_gather_twiml(BASE, call_id, "Margaret <Wilson> & Co")
    assert "Margaret &lt;Wilson&gt; &amp; Co" in twiml
    assert "<Gather" in twiml and 'numDigits="1"' in twiml
    assert f"voice/gather?call={call_id}" in twiml
    assert "Press 1" in twiml and "Press 2" in twiml and "Press 3" in twiml
    # Minimum necessary information: no addresses, no medical detail, no phone numbers.
    assert "safety alert" in twiml
    assert status_callback_url(BASE, call_id).startswith("https://")


def test_twilio_status_mapping_is_internal_and_forward_only() -> None:
    assert map_twilio_status("in-progress") is CallStatus.IN_PROGRESS
    assert map_twilio_status("Completed") is CallStatus.COMPLETED
    assert map_twilio_status("no-answer") is CallStatus.NO_ANSWER
    assert map_twilio_status("canceled") is CallStatus.CANCELLED
    assert map_twilio_status("something-new") is None
    assert may_advance(CallStatus.QUEUED, CallStatus.RINGING)
    assert may_advance(CallStatus.RINGING, CallStatus.COMPLETED)
    assert not may_advance(CallStatus.RINGING, CallStatus.QUEUED)  # replays never move back
    for terminal in TERMINAL_CALL_STATUSES:
        assert not may_advance(terminal, CallStatus.IN_PROGRESS)
        assert not may_advance(terminal, CallStatus.COMPLETED)


def test_greeting_is_configurable_and_discloses_automation() -> None:
    settings = make_settings()
    greeting = format_greeting(settings, "Margaret")
    assert greeting.startswith("Hello Margaret.")
    assert "automated" in greeting and "not a human operator" in greeting
    custom = make_settings(voice_greeting_template="Hi {preferred_name}, automated CareOS here.")
    assert format_greeting(custom, "Arthur") == "Hi Arthur, automated CareOS here."
    broken = make_settings(voice_greeting_template="Hello {unknown_field}")
    fallback = format_greeting(broken, "Margaret")
    assert "Margaret" in fallback and "automated" in fallback


def test_real_telephony_configuration_is_guarded() -> None:
    import pytest

    with pytest.raises(ValueError, match="REAL_TELEPHONY_ENABLED requires"):
        make_settings(real_telephony_enabled=True)
    with pytest.raises(ValueError, match="https"):
        make_settings(
            real_telephony_enabled=True,
            twilio_account_sid="AC0",
            twilio_auth_token="t",
            twilio_from_number="+441134960000",
            twilio_webhook_base_url="http://insecure.example",
            telephony_allowed_numbers="+447700900123",
        )
    with pytest.raises(ValueError, match="ALLOWED_NUMBERS"):
        make_settings(
            real_telephony_enabled=True,
            twilio_account_sid="AC0",
            twilio_auth_token="t",
            twilio_from_number="+441134960000",
            twilio_webhook_base_url="https://careos.example",
        )
    ok = make_settings(
        real_telephony_enabled=True,
        twilio_account_sid="AC0",
        twilio_auth_token="t",
        twilio_from_number="+441134960000",
        twilio_webhook_base_url="https://careos.example",
        telephony_allowed_numbers="+447700900123, +447700900456",
    )
    assert ok.telephony_allowed_numbers == ["+447700900123", "+447700900456"]
    assert isinstance(ok, Settings)
