"""Demo data for development and customer demos (refuses to run in staging/production).

Contains only fictional people. Phone numbers use Ofcom's ranges reserved for drama
(020 7946 0xxx, 07700 900xxx) so they can never reach a real person.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from careos.core.config import Settings
from careos.core.security import gateway_key_digest, hash_password, parse_gateway_key
from careos.core.time import utcnow
from careos.modules.device_gateway.models import GatewayCredential
from careos.modules.devices.models import ConnectionStatus, Device, DeviceConnection, DeviceType
from careos.modules.escalation_engine.policies import create_default_policy
from careos.modules.identity.models import User
from careos.modules.identity.rbac import Role
from careos.modules.organisations.models import Organisation
from careos.modules.service_users.models import ServiceUser, TrustedContact

DEMO_ORG_SLUG = "demo-care-uk"
OTHER_ORG_SLUG = "northshire-telecare"


@dataclass
class SeedReport:
    created: bool
    logins: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


async def _ensure_simulator_credential(
    session: AsyncSession, settings: Settings, organisation_id: object
) -> str:
    if settings.simulator_gateway_key is None:
        return "CAREOS_SIMULATOR_GATEWAY_KEY not set: simulator disabled"
    raw = settings.simulator_gateway_key.get_secret_value()
    parsed = parse_gateway_key(raw)
    if parsed is None:
        return "CAREOS_SIMULATOR_GATEWAY_KEY has an invalid format (cgk_<8 chars>_<secret>)"
    digest = gateway_key_digest(settings.secret_key.get_secret_value(), parsed.raw)
    credential = await session.scalar(
        select(GatewayCredential).where(GatewayCredential.key_prefix == parsed.prefix)
    )
    if credential is None:
        session.add(
            GatewayCredential(
                organisation_id=organisation_id,
                name="SOS Simulator (development)",
                key_prefix=parsed.prefix,
                key_digest=digest,
            )
        )
        return "simulator gateway credential registered"
    credential.key_digest = digest  # keep in sync if the secret or key was rotated
    return "simulator gateway credential verified"


async def seed_demo(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> SeedReport:
    if settings.is_production_like:
        raise RuntimeError("Refusing to seed demo data in a staging/production environment.")
    if settings.seed_demo_password is None:
        raise RuntimeError("Set CAREOS_SEED_DEMO_PASSWORD to seed demo users.")
    password_hash = hash_password(settings.seed_demo_password.get_secret_value())

    async with session_factory() as session:
        existing = await session.scalar(
            select(Organisation).where(Organisation.slug == DEMO_ORG_SLUG)
        )
        if existing is not None:
            note = await _ensure_simulator_credential(session, settings, existing.id)
            await session.commit()
            return SeedReport(created=False, notes=["demo data already present", note])

        now = utcnow()
        report = SeedReport(created=True)

        # ---------------------------------------------------------------- Demo Care UK
        demo = Organisation(name="Demo Care UK", slug=DEMO_ORG_SLUG)
        session.add(demo)
        await session.flush()
        await create_default_policy(session, demo.id)

        for email, name, role in (
            ("admin@democare.example.com", "Priya Shah", Role.ORGANISATION_ADMIN),
            ("manager@democare.example.com", "Daniel Hughes", Role.CARE_MANAGER),
            ("operator@democare.example.com", "Olivia Grant", Role.OPERATOR),
            ("operator2@democare.example.com", "Tom Clarke", Role.OPERATOR),
        ):
            session.add(
                User(
                    organisation_id=demo.id,
                    email=email,
                    full_name=name,
                    role=role,
                    password_hash=password_hash,
                )
            )
            report.logins.append((email, role.value))

        margaret = ServiceUser(
            organisation_id=demo.id,
            first_name="Margaret",
            last_name="Wilson",
            phone_number="+44 20 7946 0018",
            city="London",
            home_latitude=51.5074,
            home_longitude=-0.1278,
            external_reference="DCU-000123",
        )
        arthur = ServiceUser(
            organisation_id=demo.id,
            first_name="Arthur",
            last_name="Bennett",
            phone_number="+44 20 7946 0042",
            city="Manchester",
            home_latitude=53.4808,
            home_longitude=-2.2426,
        )
        doris = ServiceUser(
            organisation_id=demo.id,
            first_name="Doris",
            last_name="Palmer",
            city="Leeds",
            home_latitude=53.8008,
            home_longitude=-1.5491,
        )
        session.add_all([margaret, arthur, doris])
        await session.flush()

        session.add_all(
            [
                TrustedContact(
                    organisation_id=demo.id,
                    service_user_id=margaret.id,
                    full_name="Sarah Wilson",
                    relationship="Daughter",
                    phone_number="+44 7700 900123",
                    priority=1,
                ),
                TrustedContact(
                    organisation_id=demo.id,
                    service_user_id=margaret.id,
                    full_name="James Wilson",
                    relationship="Son",
                    phone_number="+44 7700 900456",
                    priority=2,
                ),
                TrustedContact(
                    organisation_id=demo.id,
                    service_user_id=arthur.id,
                    full_name="Helen Bennett",
                    relationship="Wife",
                    phone_number="+44 7700 900789",
                    priority=1,
                ),
            ]
        )

        for external_id, owner, battery, signal, status, last_seen in (
            ("DEV-0001", margaret, 84, 92, ConnectionStatus.ONLINE, now),
            ("DEV-0002", arthur, 14, 71, ConnectionStatus.ONLINE, now - timedelta(minutes=12)),
            ("DEV-0003", doris, 58, 40, ConnectionStatus.OFFLINE, now - timedelta(days=2)),
        ):
            device = Device(
                organisation_id=demo.id,
                service_user_id=owner.id,
                external_id=external_id,
                device_type=DeviceType.SOS_PENDANT,
                manufacturer="CareOS Sim",
                model="Sim Pendant v1",
                adapter="simulator",
            )
            session.add(device)
            await session.flush()
            session.add(
                DeviceConnection(
                    organisation_id=demo.id,
                    device_id=device.id,
                    status=status,
                    last_seen_at=last_seen,
                    battery_level=battery,
                    signal_strength=signal,
                    last_latitude=owner.home_latitude,
                    last_longitude=owner.home_longitude,
                )
            )

        report.notes.append(await _ensure_simulator_credential(session, settings, demo.id))

        # ---------------------------------------------- second tenant (isolation demo)
        other = Organisation(name="Northshire Telecare Ltd", slug=OTHER_ORG_SLUG)
        session.add(other)
        await session.flush()
        await create_default_policy(session, other.id)
        session.add(
            User(
                organisation_id=other.id,
                email="operator@northshire.example.com",
                full_name="Mark Ellis",
                role=Role.OPERATOR,
                password_hash=password_hash,
            )
        )
        report.logins.append(("operator@northshire.example.com", Role.OPERATOR.value))
        edward = ServiceUser(
            organisation_id=other.id, first_name="Edward", last_name="Collins", city="York"
        )
        session.add(edward)
        await session.flush()
        session.add(
            Device(
                organisation_id=other.id,
                service_user_id=edward.id,
                external_id="NTH-0001",
                device_type=DeviceType.SOS_PENDANT,
                manufacturer="CareOS Sim",
                adapter="simulator",
            )
        )

        session.add(
            User(
                organisation_id=None,
                email="platform@careos.example.com",
                full_name="Platform Administrator",
                role=Role.PLATFORM_ADMIN,
                password_hash=password_hash,
            )
        )
        report.logins.append(("platform@careos.example.com", Role.PLATFORM_ADMIN.value))
        await session.commit()
        return report
