from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from careos.cli import export_contracts
from careos.contracts.device_events import CareOSEvent, DeviceEventType
from careos.core.errors import NotFoundError, ValidationFailedError
from careos.modules.device_gateway.adapters import SimulatorPendantAdapter, default_adapters
from tests.factories import sos_event

_PARENTS = Path(__file__).resolve().parents
CONTRACTS_DIR = _PARENTS[4] / "packages" / "contracts" if len(_PARENTS) > 4 else None


def test_valid_event_is_normalised_to_utc() -> None:
    payload = sos_event("DEV-0001", timestamp="2026-09-16T10:30:00+01:00")
    event = CareOSEvent.model_validate(payload)
    assert event.event_type is DeviceEventType.SOS_BUTTON
    assert event.timestamp == datetime(2026, 9, 16, 9, 30, tzinfo=UTC)
    assert event.raises_incident


@pytest.mark.parametrize(
    "overrides",
    [
        {"unexpected": "field"},
        {"event_id": "short"},
        {"event_type": "PANIC"},
        {"device": {"battery": 140}},
        {"location": {"latitude": 123, "longitude": 0}},
        {"timestamp": "2026-09-16T10:30:00"},  # naive timestamps are ambiguous
        {"metadata": {"k": "x" * 300}},
    ],
)
def test_invalid_events_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CareOSEvent.model_validate(sos_event("DEV-0001", **overrides))


def test_future_timestamps_beyond_clock_skew_are_rejected() -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    with pytest.raises(ValidationError):
        CareOSEvent.model_validate(sos_event("DEV-0001", timestamp=future))


def test_simulator_adapter_maps_vendor_format() -> None:
    event = SimulatorPendantAdapter().normalise(
        {
            "msg_id": "sim_0123456789",
            "imei": "DEV-0001",
            "kind": "SOS",
            "ts_ms": int(datetime.now(UTC).timestamp() * 1000),
            "bat_pct": 84,
            "rssi_pct": 92,
            "gps": {"lat": 51.5, "lon": -0.12},
        }
    )
    assert event.event_type is DeviceEventType.SOS_BUTTON
    assert event.device.battery == 84
    assert event.location is not None and event.location.latitude == 51.5


def test_adapter_errors_do_not_echo_payload_values() -> None:
    with pytest.raises(ValidationFailedError) as exc:
        SimulatorPendantAdapter().normalise({"msg_id": "sim_1", "imei": "Margaret's pendant"})
    assert "Margaret" not in json.dumps(exc.value.details)


def test_unknown_adapter() -> None:
    with pytest.raises(NotFoundError):
        default_adapters().get("acme-unknown")


@pytest.mark.skipif(
    CONTRACTS_DIR is None or not CONTRACTS_DIR.exists(),
    reason="packages/contracts is not available (e.g. inside the backend container image)",
)
def test_published_json_schemas_are_up_to_date(tmp_path: Path) -> None:
    """Drift check: packages/contracts must be regenerated when the Pydantic contracts change."""
    assert CONTRACTS_DIR is not None
    for generated in export_contracts(tmp_path):
        committed = CONTRACTS_DIR / "schemas" / generated.name
        assert committed.exists(), (
            f"missing {committed}; run `python -m careos.cli export-contracts`"
        )
        assert json.loads(committed.read_text()) == json.loads(generated.read_text()), (
            f"{generated.name} is stale; run `python -m careos.cli export-contracts`"
        )
