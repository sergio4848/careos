"""Device Event Gateway.

    vendor payload -> adapter -> CareOSEvent -> idempotency ledger -> device registry
                   -> telemetry snapshot -> Incident Engine  (one transaction)

The gateway authenticates the *sending platform* (a gateway credential bound to one
organisation). Devices are resolved only inside that organisation, so a credential can
never raise an alarm for another tenant's device.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, NoReturn

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from careos.contracts.device_events import CareOSEvent
from careos.contracts.realtime import RealtimeMessage, RealtimeMessageType
from careos.core.config import Settings
from careos.core.errors import (
    AuthenticationRequiredError,
    ConflictError,
    RateLimitedError,
    ValidationFailedError,
)
from careos.core.metrics import GATEWAY_EVENTS
from careos.core.rate_limit import RateLimiter
from careos.core.security import constant_time_equals, gateway_key_digest, parse_gateway_key
from careos.core.time import utcnow
from careos.db.uow import UnitOfWork
from careos.modules.audit import service as audit
from careos.modules.audit.models import AuditActorType, AuditOutcome
from careos.modules.audit.service import AuditAction, AuditContext, AuditEntry
from careos.modules.device_gateway.adapters import AdapterRegistry
from careos.modules.device_gateway.models import (
    DeviceEventReceipt,
    GatewayCredential,
    ReceiptOutcome,
)
from careos.modules.devices.models import ConnectionStatus, Device, DeviceConnection
from careos.modules.incident_engine.models import Incident
from careos.modules.incident_engine.service import IncidentEngine
from careos.modules.organisations.models import Organisation, OrganisationStatus

GATEWAY_KEY_HEADER = "X-CareOS-Gateway-Key"


class EventIdConflictError(ConflictError):
    code = "event_id_conflict"
    default_message = "This event_id was already used for a different payload."


@dataclass(frozen=True, slots=True)
class IngestResult:
    receipt_id: uuid.UUID
    event_id: str
    duplicate: bool
    outcome: ReceiptOutcome | None
    incident_id: uuid.UUID | None
    incident_reference: str | None
    incident_status: str | None


def payload_digest(event: CareOSEvent) -> tuple[dict[str, Any], str]:
    canonical = event.model_dump(mode="json")
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return canonical, hashlib.sha256(encoded).hexdigest()


class DeviceGatewayService:
    def __init__(
        self,
        *,
        settings: Settings,
        adapters: AdapterRegistry,
        engine: IncidentEngine,
        rate_limiter: RateLimiter,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._adapters = adapters
        self._engine = engine
        self._limiter = rate_limiter
        self._session_factory = session_factory

    # ------------------------------------------------------------------ authentication

    async def authenticate(
        self, session: AsyncSession, raw_key: str | None, context: AuditContext
    ) -> GatewayCredential:
        parsed = parse_gateway_key(raw_key) if raw_key else None
        credential: GatewayCredential | None = None
        if parsed is not None:
            credential = await session.scalar(
                select(GatewayCredential)
                .join(Organisation, Organisation.id == GatewayCredential.organisation_id)
                .where(
                    GatewayCredential.key_prefix == parsed.prefix,
                    GatewayCredential.revoked_at.is_(None),
                    Organisation.status == OrganisationStatus.ACTIVE,
                    Organisation.deleted_at.is_(None),
                )
            )
        secret = self._settings.secret_key.get_secret_value()
        digest = gateway_key_digest(secret, parsed.raw) if parsed else ""
        if credential is None or not constant_time_equals(credential.key_digest, digest):
            await audit.record_isolated(
                self._session_factory,
                AuditEntry(
                    action=AuditAction.GATEWAY_AUTH_FAILED,
                    outcome=AuditOutcome.DENIED,
                    actor_type=AuditActorType.GATEWAY,
                    details={"key_prefix": parsed.prefix if parsed else None},
                ),
                context,
            )
            raise AuthenticationRequiredError("Invalid gateway credential.")

        decision = await self._limiter.hit(
            f"gateway:{credential.id}", self._settings.gateway_max_requests_per_minute, 60
        )
        if not decision.allowed:
            raise RateLimitedError(decision.retry_after_seconds)
        now = utcnow()
        if credential.last_used_at is None or now - credential.last_used_at > timedelta(minutes=1):
            credential.last_used_at = now
            await session.commit()
        return credential

    # ------------------------------------------------------------------ ingestion

    async def ingest(
        self,
        uow: UnitOfWork,
        credential: GatewayCredential,
        adapter_name: str,
        payload: Mapping[str, Any],
        context: AuditContext,
    ) -> IngestResult:
        session = uow.session
        organisation_id = credential.organisation_id
        adapter = self._adapters.get(adapter_name)
        try:
            event = adapter.normalise(payload)
        except ValidationFailedError:
            GATEWAY_EVENTS.labels(adapter_name, "unknown", "invalid").inc()
            await self._audit_rejection(
                organisation_id, adapter_name, None, "invalid_payload", context
            )
            raise

        canonical, digest = payload_digest(event)
        now = utcnow()
        receipt_id = await session.scalar(
            pg_insert(DeviceEventReceipt)
            .values(
                id=uuid.uuid4(),
                organisation_id=organisation_id,
                event_id=event.event_id,
                adapter=adapter_name,
                event_type=event.event_type.value,
                device_external_id=event.device_id,
                payload_sha256=digest,
                payload=canonical,
                occurred_at=event.timestamp,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_device_event_receipts_org_event")
            .returning(DeviceEventReceipt.id)
        )
        if receipt_id is None:
            try:
                return await self._duplicate(session, organisation_id, adapter_name, event, digest)
            except EventIdConflictError:
                await self._audit_rejection(
                    organisation_id, adapter_name, event, "event_id_conflict", context
                )
                raise

        device = await session.scalar(
            select(Device)
            .where(
                Device.organisation_id == organisation_id,
                Device.external_id == event.device_id,
                Device.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if device is None:
            await uow.rollback()
            await self._reject(organisation_id, adapter_name, event, "unknown_device", context)
        if event.service_user_id is not None and event.service_user_id != device.service_user_id:
            await uow.rollback()
            await self._reject(
                organisation_id, adapter_name, event, "service_user_mismatch", context
            )

        await self._record_telemetry(session, device, event)
        outcome = await self._engine.handle_device_event(
            uow, device=device, event=event, receipt_id=receipt_id
        )
        incident = outcome.incident
        await session.execute(
            update(DeviceEventReceipt)
            .where(DeviceEventReceipt.id == receipt_id)
            .values(
                device_id=device.id,
                incident_id=incident.id if incident else None,
                outcome=outcome.outcome,
            )
        )
        uow.notify(
            RealtimeMessage(
                type=RealtimeMessageType.DEVICE_UPDATED,
                organisation_id=organisation_id,
                device_id=device.id,
                payload={"event_type": event.event_type.value},
            )
        )
        await uow.commit()
        GATEWAY_EVENTS.labels(adapter_name, event.event_type.value, outcome.outcome.value).inc()
        return IngestResult(
            receipt_id=receipt_id,
            event_id=event.event_id,
            duplicate=False,
            outcome=outcome.outcome,
            incident_id=incident.id if incident else None,
            incident_reference=incident.reference if incident else None,
            incident_status=incident.status.value if incident else None,
        )

    async def _duplicate(
        self,
        session: AsyncSession,
        organisation_id: uuid.UUID,
        adapter_name: str,
        event: CareOSEvent,
        digest: str,
    ) -> IngestResult:
        existing = await session.scalar(
            select(DeviceEventReceipt).where(
                DeviceEventReceipt.organisation_id == organisation_id,
                DeviceEventReceipt.event_id == event.event_id,
            )
        )
        if existing is None:  # pragma: no cover - conflict implies the row exists
            raise ConflictError()
        if existing.payload_sha256 != digest:
            GATEWAY_EVENTS.labels(adapter_name, event.event_type.value, "event_id_conflict").inc()
            raise EventIdConflictError()
        GATEWAY_EVENTS.labels(adapter_name, event.event_type.value, "duplicate").inc()
        incident = (
            await session.get(Incident, existing.incident_id) if existing.incident_id else None
        )
        return IngestResult(
            receipt_id=existing.id,
            event_id=existing.event_id,
            duplicate=True,
            outcome=existing.outcome,
            incident_id=existing.incident_id,
            incident_reference=incident.reference if incident else None,
            incident_status=incident.status.value if incident else None,
        )

    async def _record_telemetry(
        self, session: AsyncSession, device: Device, event: CareOSEvent
    ) -> None:
        now = utcnow()
        values: dict[str, Any] = {
            "status": ConnectionStatus.ONLINE,
            "last_seen_at": now,
            "updated_at": now,
        }
        if event.device.battery is not None:
            values["battery_level"] = event.device.battery
        if event.device.signal is not None:
            values["signal_strength"] = event.device.signal
        if event.location is not None:
            values["last_latitude"] = event.location.latitude
            values["last_longitude"] = event.location.longitude
        stmt = pg_insert(DeviceConnection).values(
            id=uuid.uuid4(),
            organisation_id=device.organisation_id,
            device_id=device.id,
            created_at=now,
            **values,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_device_connections_device_id",
                set_={**values, "last_seen_at": func.greatest(DeviceConnection.last_seen_at, now)},
            )
        )

    async def _reject(
        self,
        organisation_id: uuid.UUID,
        adapter_name: str,
        event: CareOSEvent,
        reason: str,
        context: AuditContext,
    ) -> NoReturn:
        GATEWAY_EVENTS.labels(adapter_name, event.event_type.value, reason).inc()
        await self._audit_rejection(organisation_id, adapter_name, event, reason, context)
        raise ValidationFailedError("Device event rejected.", details={"reason": reason})

    async def _audit_rejection(
        self,
        organisation_id: uuid.UUID,
        adapter_name: str,
        event: CareOSEvent | None,
        reason: str,
        context: AuditContext,
    ) -> None:
        await audit.record_isolated(
            self._session_factory,
            AuditEntry(
                action=AuditAction.GATEWAY_EVENT_REJECTED,
                outcome=AuditOutcome.FAILURE,
                organisation_id=organisation_id,
                actor_type=AuditActorType.GATEWAY,
                resource_type="device_event",
                resource_id=event.event_id[:64] if event else None,
                details={
                    "reason": reason,
                    "adapter": adapter_name,
                    "event_type": event.event_type.value if event else None,
                    "device_external_id": event.device_id if event else None,
                },
            ),
            context,
        )
