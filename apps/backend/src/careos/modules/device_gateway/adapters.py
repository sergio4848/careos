"""Manufacturer adapters: vendor payload -> CareOS Event Contract.

    Device -> Manufacturer Adapter -> CareOSEvent -> Event Gateway -> Incident Engine

Adding a manufacturer (or a SCAIP / TS 50134-9 receiver) means adding an adapter here;
the gateway and the Incident Engine do not change.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from careos.contracts.device_events import CareOSEvent, DeviceEventType
from careos.core.errors import NotFoundError, ValidationFailedError


class DeviceAdapter(Protocol):
    name: str

    def normalise(self, payload: Mapping[str, Any]) -> CareOSEvent: ...


def _validation_error(exc: ValidationError) -> ValidationFailedError:
    # Only field locations and error types: never echo payload values (may contain PII).
    problems = [
        {"loc": ".".join(str(p) for p in e["loc"]), "type": e["type"]} for e in exc.errors()
    ]
    return ValidationFailedError("Device event failed validation.", details={"errors": problems})


class CareOSNativeAdapter:
    """For platforms that already speak the CareOS contract."""

    name = "careos"

    def normalise(self, payload: Mapping[str, Any]) -> CareOSEvent:
        try:
            return CareOSEvent.model_validate(payload)
        except ValidationError as exc:
            raise _validation_error(exc) from exc


class _SimulatorGps(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float
    lon: float


class _SimulatorPayload(BaseModel):
    """Vendor format of the fictional 'CareOS Sim Pendant' used by the SOS simulator.

    Deliberately different from the CareOS contract so the adapter path is exercised.
    """

    model_config = ConfigDict(extra="forbid")

    msg_id: str
    imei: str
    kind: str = Field(pattern=r"^(SOS|FALL|FAULT|LOW_BATT|HEARTBEAT)$")
    ts_ms: int = Field(ge=0)
    bat_pct: int | None = None
    rssi_pct: int | None = None
    gps: _SimulatorGps | None = None
    fw: str | None = Field(default=None, max_length=32)


_SIMULATOR_KINDS = {
    "SOS": DeviceEventType.SOS_BUTTON,
    "FALL": DeviceEventType.FALL_DETECTED,
    "FAULT": DeviceEventType.DEVICE_FAULT,
    "LOW_BATT": DeviceEventType.LOW_BATTERY,
    "HEARTBEAT": DeviceEventType.HEARTBEAT,
}


class SimulatorPendantAdapter:
    name = "simulator"

    def normalise(self, payload: Mapping[str, Any]) -> CareOSEvent:
        try:
            vendor = _SimulatorPayload.model_validate(payload)
            return CareOSEvent.model_validate(
                {
                    "event_id": vendor.msg_id,
                    "event_type": _SIMULATOR_KINDS[vendor.kind],
                    "device_id": vendor.imei,
                    "timestamp": datetime.fromtimestamp(vendor.ts_ms / 1000, tz=UTC),
                    "location": (
                        {"latitude": vendor.gps.lat, "longitude": vendor.gps.lon}
                        if vendor.gps
                        else None
                    ),
                    "device": {"battery": vendor.bat_pct, "signal": vendor.rssi_pct},
                    "metadata": {"firmware": vendor.fw} if vendor.fw else {},
                }
            )
        except ValidationError as exc:
            raise _validation_error(exc) from exc


class AdapterRegistry:
    def __init__(self, adapters: list[DeviceAdapter]) -> None:
        self._adapters = {adapter.name: adapter for adapter in adapters}

    def get(self, name: str) -> DeviceAdapter:
        adapter = self._adapters.get(name)
        if adapter is None:
            raise NotFoundError("Unknown device adapter.")
        return adapter

    @property
    def names(self) -> list[str]:
        return sorted(self._adapters)


def default_adapters() -> AdapterRegistry:
    return AdapterRegistry([CareOSNativeAdapter(), SimulatorPendantAdapter()])
