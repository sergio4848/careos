"""Test data builders. Each tenant gets users for every role, a service user with two
trusted contacts, a registered SOS pendant, the default escalation policy and a gateway key."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from careos.core.config import Settings
from careos.core.security import gateway_key_digest, hash_password
from careos.modules.device_gateway.models import GatewayCredential
from careos.modules.devices.models import ConnectionStatus, Device, DeviceConnection, DeviceType
from careos.modules.escalation_engine.policies import create_default_policy
from careos.modules.identity.models import User
from careos.modules.identity.rbac import Role
from careos.modules.organisations.models import Organisation
from careos.modules.service_users.models import ServiceUser, TrustedContact

ClientFactory = Callable[..., Awaitable[httpx.AsyncClient]]

TEST_PASSWORD = "Correct-Horse-Battery-9"
SIMULATOR_KEY = "cgk_demo0001_simulator-test-secret-value-0001"

_password_hash = hash_password(TEST_PASSWORD)


@dataclass
class Tenant:
    organisation_id: uuid.UUID
    slug: str
    gateway_key: str
    service_user_id: uuid.UUID
    device_id: uuid.UUID
    device_external_id: str
    unassigned_device_external_id: str
    emails: dict[Role, str] = field(default_factory=dict)
    second_operator_email: str = ""


class TenantFactory:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def create_tenant(self, *, slug: str, name: str, key_prefix: str) -> Tenant:
        secret = self._settings.secret_key.get_secret_value()
        gateway_key = (
            SIMULATOR_KEY
            if key_prefix == "demo0001"
            else f"cgk_{key_prefix}_tenant-test-secret-{slug}"
        )
        async with self._session_factory() as session:
            organisation = Organisation(name=name, slug=slug)
            session.add(organisation)
            await session.flush()
            org_id = organisation.id
            await create_default_policy(session, org_id)

            emails: dict[Role, str] = {}
            for role in (
                Role.ORGANISATION_ADMIN,
                Role.CARE_MANAGER,
                Role.OPERATOR,
                Role.CAREGIVER,
                Role.TRUSTED_CONTACT,
            ):
                email = f"{role.value.lower()}@{slug}.example.com"
                emails[role] = email
                session.add(
                    User(
                        organisation_id=org_id,
                        email=email,
                        full_name=f"{role.value.title()} {slug}",
                        role=role,
                        password_hash=_password_hash,
                    )
                )
            second_operator = f"operator2@{slug}.example.com"
            session.add(
                User(
                    organisation_id=org_id,
                    email=second_operator,
                    full_name=f"Second Operator {slug}",
                    role=Role.OPERATOR,
                    password_hash=_password_hash,
                )
            )

            service_user = ServiceUser(
                organisation_id=org_id,
                first_name="Margaret",
                last_name="Wilson",
                phone_number="+44 20 7946 0018",
                city="London",
                home_latitude=51.5074,
                home_longitude=-0.1278,
            )
            session.add(service_user)
            await session.flush()
            session.add_all(
                [
                    TrustedContact(
                        organisation_id=org_id,
                        service_user_id=service_user.id,
                        full_name="Sarah Wilson",
                        relationship="Daughter",
                        phone_number="+44 7700 900123",
                        priority=1,
                    ),
                    TrustedContact(
                        organisation_id=org_id,
                        service_user_id=service_user.id,
                        full_name="James Wilson",
                        relationship="Son",
                        phone_number="+44 7700 900456",
                        priority=2,
                    ),
                ]
            )
            external_id = f"DEV-{slug[:4].upper()}-1"
            device = Device(
                organisation_id=org_id,
                service_user_id=service_user.id,
                external_id=external_id,
                device_type=DeviceType.SOS_PENDANT,
                manufacturer="CareOS Sim",
                adapter="simulator",
            )
            unassigned = Device(
                organisation_id=org_id,
                service_user_id=None,
                external_id=f"DEV-{slug[:4].upper()}-SPARE",
                device_type=DeviceType.SOS_PENDANT,
                manufacturer="CareOS Sim",
                adapter="simulator",
            )
            session.add_all([device, unassigned])
            await session.flush()
            session.add(
                DeviceConnection(
                    organisation_id=org_id,
                    device_id=device.id,
                    status=ConnectionStatus.ONLINE,
                    battery_level=84,
                    signal_strength=92,
                    last_seen_at=datetime.now(UTC),
                )
            )
            session.add(
                GatewayCredential(
                    organisation_id=org_id,
                    name="test credential",
                    key_prefix=key_prefix,
                    key_digest=gateway_key_digest(secret, gateway_key),
                )
            )
            await session.commit()
            return Tenant(
                organisation_id=org_id,
                slug=slug,
                gateway_key=gateway_key,
                service_user_id=service_user.id,
                device_id=device.id,
                device_external_id=external_id,
                unassigned_device_external_id=unassigned.external_id,
                emails=emails,
                second_operator_email=second_operator,
            )


def sos_event(device_external_id: str, **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event_type": "SOS_BUTTON",
        "device_id": device_external_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "location": {"latitude": 51.5074, "longitude": -0.1278},
        "device": {"battery": 74, "signal": 82},
        "metadata": {},
    }
    event.update(overrides)
    return event
