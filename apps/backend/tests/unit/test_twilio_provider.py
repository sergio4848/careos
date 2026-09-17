"""TwilioVoiceProvider against a fake Twilio API and a fake call store. No network."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

import httpx
import pytest

from careos.modules.notification_engine.models import CallStatus
from careos.modules.notification_engine.providers import (
    ProviderError,
    VoiceCallOutcome,
    VoiceCallRequest,
)
from careos.modules.telephony.provider import TwilioVoiceProvider
from careos.modules.telephony.store import CallSnapshot
from tests.conftest import make_settings

ALLOWED = "+447700900123"


def twilio_settings(**overrides: object):
    values: dict[str, object] = {
        "real_telephony_enabled": True,
        "twilio_account_sid": "AC00000000000000000000000000000000",
        "twilio_auth_token": "twilio-auth-token-test",
        "twilio_from_number": "+441134960000",
        "twilio_webhook_base_url": "https://careos.example",
        "telephony_allowed_numbers": ALLOWED,
        "twilio_api_timeout_seconds": 1,
        "twilio_ring_timeout_seconds": 5,
        "voice_call_max_duration_seconds": 10,
        "voice_call_poll_interval_seconds": 0.01,
    }
    values.update(overrides)
    return make_settings(**values)


@dataclass
class FakeStore:
    """Scripted call state, as the webhooks/media bridge would have written it."""

    snapshots: list[CallSnapshot] = field(default_factory=list)
    bound: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    timed_out: list[uuid.UUID] = field(default_factory=list)

    async def bind_provider_sid(self, call_id, sid, *, from_number_masked):
        assert "*" in (from_number_masked or ""), "from number must be masked"
        self.bound.append((call_id, sid))

    async def snapshot(self, call_id):
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0] if self.snapshots else None

    async def mark_timed_out(self, call_id):
        self.timed_out.append(call_id)
        self.snapshots = [snap(CallStatus.TIMED_OUT)]


def snap(status: CallStatus, *, acknowledged: bool = False, answered: bool = False) -> CallSnapshot:
    return CallSnapshot(
        status=status,
        acknowledged=acknowledged,
        provider_call_sid="CAfixed",
        structured_response=None,
        answered=answered,
    )


class FakeTwilioAPI:
    def __init__(self, status_code: int = 201, hang: bool = False) -> None:
        self.status_code = status_code
        self.hang = hang
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.hang:
                # MockTransport bypasses the socket layer, so surface what a timeout
                # looks like to httpx callers instead of actually sleeping.
                raise httpx.ConnectTimeout("simulated Twilio API timeout")
            if self.status_code >= 400:
                return httpx.Response(
                    self.status_code,
                    json={"code": 21211, "message": "invalid"},
                )
            return httpx.Response(self.status_code, json={"sid": "CA" + "1" * 32})

        return httpx.MockTransport(handler)


def request(purpose: str = "AUTOMATED_WELFARE_CHECK", to: str = ALLOWED) -> VoiceCallRequest:
    return VoiceCallRequest(
        organisation_id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        call_id=uuid.uuid4(),
        to_number=to,
        purpose=purpose,  # type: ignore[arg-type]
        media_token="tok-media-secret" if purpose == "AUTOMATED_WELFARE_CHECK" else None,
        subject_name="Margaret Wilson" if purpose == "TRUSTED_CONTACT_ALERT" else None,
    )


def provider(settings=None, api: FakeTwilioAPI | None = None, store: FakeStore | None = None):
    api = api or FakeTwilioAPI()
    store = store or FakeStore(snapshots=[snap(CallStatus.COMPLETED, answered=True)])
    p = TwilioVoiceProvider(settings or twilio_settings(), store, transport=api.transport())  # type: ignore[arg-type]
    return p, api, store


# ------------------------------------------------------------------ safety switches


async def test_refuses_every_call_when_real_telephony_is_disabled() -> None:
    p, api, _ = provider(
        make_settings(
            twilio_account_sid="AC0",
            twilio_auth_token="t",
            twilio_from_number="+441134960000",
            twilio_webhook_base_url="https://careos.example",
            telephony_allowed_numbers=ALLOWED,
            # real_telephony_enabled stays False (the default)
        )
    )
    with pytest.raises(ProviderError, match="REAL_TELEPHONY_ENABLED"):
        await p.place_call(request())
    assert api.requests == []  # Twilio was never contacted


async def test_refuses_numbers_outside_the_development_allowlist() -> None:
    p, api, _ = provider()
    with pytest.raises(ProviderError, match="ALLOWED_NUMBERS"):
        await p.place_call(request(to="+447700900999"))
    assert api.requests == []


async def test_refuses_invalid_e164_numbers() -> None:
    p, api, _ = provider(twilio_settings(telephony_allowed_numbers="+44 20 7946 0018"))
    with pytest.raises(ProviderError, match=r"E\.164"):
        await p.place_call(request(to="+44 20 7946 0018"))
    assert api.requests == []


# ------------------------------------------------------------------ call creation


async def test_outbound_call_creation_uses_region_edge_and_signed_callbacks() -> None:
    p, api, store = provider()
    req = request()
    result = await p.place_call(req)

    (created,) = [r for r in api.requests if r.url.path.endswith("/Calls.json")]
    assert created.url.host == "api.dublin.ie1.twilio.com"  # UK-first: EU region, not US
    form = dict(httpx.QueryParams(created.content.decode()))
    assert form["To"] == ALLOWED and form["From"] == "+441134960000"
    assert form["Timeout"] == "5" and form["TimeLimit"] == "10"
    assert (
        form["StatusCallback"]
        == f"https://careos.example/v1/providers/twilio/voice/status?call={req.call_id}"
    )
    assert "wss://careos.example/v1/providers/twilio/media" in form["Twiml"]
    assert 'name="token" value="tok-media-secret"' in form["Twiml"]
    assert store.bound == [(req.call_id, "CA" + "1" * 32)]
    assert result.outcome is VoiceCallOutcome.ANSWERED


async def test_trusted_contact_call_uses_deterministic_gather_twiml() -> None:
    p, api, _ = provider()
    await p.place_call(request(purpose="TRUSTED_CONTACT_ALERT"))
    form = dict(httpx.QueryParams(api.requests[0].content.decode()))
    assert "<Gather" in form["Twiml"] and "Margaret Wilson" in form["Twiml"]
    assert "<Stream" not in form["Twiml"]  # no media/AI on trusted-contact calls


async def test_default_region_is_not_hardcoded_us() -> None:
    p, api, _ = provider(twilio_settings(twilio_region="", twilio_edge=""))
    await p.place_call(request())
    assert api.requests[0].url.host == "api.twilio.com"  # global fallback, configurable


# ------------------------------------------------------------------ provider failures


async def test_twilio_api_timeout_raises_timeout_error() -> None:
    p, _api, _ = provider(api=FakeTwilioAPI(hang=True))
    with pytest.raises(TimeoutError):
        await p.place_call(request())


async def test_twilio_api_rejection_raises_provider_error() -> None:
    p, _, _ = provider(api=FakeTwilioAPI(status_code=400))
    with pytest.raises(ProviderError, match="21211"):
        await p.place_call(request())


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (CallStatus.BUSY, VoiceCallOutcome.BUSY),
        (CallStatus.NO_ANSWER, VoiceCallOutcome.NO_ANSWER),
        (CallStatus.CANCELLED, VoiceCallOutcome.CANCELLED),
    ],
)
async def test_unanswered_outcomes_map_from_webhook_state(status, outcome) -> None:
    p, _, _ = provider(store=FakeStore(snapshots=[snap(status)]))
    result = await p.place_call(request())
    assert result.outcome is outcome and result.acknowledged is False


async def test_completed_without_answer_is_no_answer() -> None:
    p, _, _ = provider(store=FakeStore(snapshots=[snap(CallStatus.COMPLETED, answered=False)]))
    result = await p.place_call(request())
    assert result.outcome is VoiceCallOutcome.NO_ANSWER


async def test_failed_call_raises_for_retry_and_failsafe() -> None:
    p, _, _ = provider(store=FakeStore(snapshots=[snap(CallStatus.FAILED)]))
    with pytest.raises(ProviderError, match="failed"):
        await p.place_call(request())


async def test_max_duration_hangs_up_and_times_out_deliberately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    store = FakeStore(snapshots=[snap(CallStatus.IN_PROGRESS)])  # never terminal by itself
    api = FakeTwilioAPI()
    p = TwilioVoiceProvider(twilio_settings(), store, transport=api.transport())  # type: ignore[arg-type]
    clock = iter([0.0, 0.0, 10_000.0])
    monkeypatch.setattr(
        "careos.modules.telephony.provider.time",
        types.SimpleNamespace(monotonic=lambda: next(clock, 10_000.0)),
    )
    result = await p.place_call(request())
    assert result.outcome is VoiceCallOutcome.TIMED_OUT
    assert store.timed_out, "the timeout must be recorded deliberately"
    hangups = [r for r in api.requests if "/Calls/CA" in str(r.url)]
    assert len(hangups) == 1, "the runaway call must be hung up"


async def test_cancel_call_requests_provider_hangup() -> None:
    p, api, _ = provider()
    await p.cancel_call("CAdead")
    hangups = [r for r in api.requests if r.url.path.endswith("/Calls/CAdead.json")]
    assert len(hangups) == 1
    assert dict(httpx.QueryParams(hangups[0].content.decode()))["Status"] == "completed"


async def test_operation_deadline_reflects_configuration() -> None:
    p, _, _ = provider(
        twilio_settings(
            twilio_api_timeout_seconds=2,
            twilio_ring_timeout_seconds=20,
            voice_call_max_duration_seconds=120,
        )
    )
    assert p.operation_deadline_seconds == 2 + 20 + 120 + 15


async def test_no_full_number_appears_in_twiml_or_masked_fields() -> None:
    p, api, store = provider()
    await p.place_call(request())
    form = dict(httpx.QueryParams(api.requests[0].content.decode()))
    twiml = form["Twiml"]
    # TwiML for welfare calls carries no phone numbers at all.
    assert ALLOWED not in twiml and "+441134960000" not in twiml
    assert json.dumps(store.bound, default=str).count("+44") == 0
