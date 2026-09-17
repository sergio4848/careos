from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careos.core.errors import ConflictError, NotFoundError
from careos.modules.audit import service as audit
from careos.modules.audit.service import AuditAction, AuditContext, AuditEntry
from careos.modules.devices.service import list_devices
from careos.modules.escalation_engine.models import EscalationPolicy
from careos.modules.identity.principal import Principal
from careos.modules.incident_engine.models import Incident
from careos.modules.incident_engine.queries import build_summaries
from careos.modules.incident_engine.state_machine import ACTIVE_STATUSES
from careos.modules.service_users.models import ServiceUser, TrustedContact
from careos.modules.service_users.schemas import (
    ServiceUserProfile,
    ServiceUserSummary,
    ServiceUserWrite,
    TrustedContactView,
    TrustedContactWrite,
)


async def _get(
    session: AsyncSession, principal: Principal, service_user_id: uuid.UUID
) -> ServiceUser:
    service_user = await session.scalar(
        select(ServiceUser).where(
            ServiceUser.id == service_user_id,
            ServiceUser.organisation_id == principal.tenant_id,
            ServiceUser.deleted_at.is_(None),
        )
    )
    if service_user is None:
        raise NotFoundError()
    return service_user


async def _check_policy(
    session: AsyncSession, principal: Principal, policy_id: uuid.UUID | None
) -> None:
    if policy_id is None:
        return
    found = await session.scalar(
        select(EscalationPolicy.id).where(
            EscalationPolicy.id == policy_id,
            EscalationPolicy.organisation_id == principal.tenant_id,
            EscalationPolicy.deleted_at.is_(None),
        )
    )
    if found is None:
        raise NotFoundError("Escalation policy not found.")


async def list_service_users(
    session: AsyncSession, principal: Principal
) -> list[ServiceUserSummary]:
    active_counts = (
        select(Incident.service_user_id, func.count().label("n"))
        .where(
            Incident.organisation_id == principal.tenant_id, Incident.status.in_(ACTIVE_STATUSES)
        )
        .group_by(Incident.service_user_id)
        .subquery()
    )
    rows = await session.execute(
        select(ServiceUser, func.coalesce(active_counts.c.n, 0))
        .outerjoin(active_counts, active_counts.c.service_user_id == ServiceUser.id)
        .where(ServiceUser.organisation_id == principal.tenant_id, ServiceUser.deleted_at.is_(None))
        .order_by(ServiceUser.last_name, ServiceUser.first_name)
    )
    return [
        ServiceUserSummary(
            id=su.id,
            display_name=su.display_name,
            first_name=su.first_name,
            last_name=su.last_name,
            city=su.city,
            status=su.status,
            active_incident_count=int(count),
        )
        for su, count in rows.tuples()
    ]


async def get_profile(
    session: AsyncSession,
    principal: Principal,
    service_user_id: uuid.UUID,
    *,
    low_battery_threshold: int,
    context: AuditContext,
) -> ServiceUserProfile:
    su = await _get(session, principal, service_user_id)
    contacts = (
        await session.execute(
            select(TrustedContact)
            .where(TrustedContact.service_user_id == su.id, TrustedContact.deleted_at.is_(None))
            .order_by(TrustedContact.priority)
        )
    ).scalars()
    incidents = (
        await session.execute(
            select(Incident)
            .where(
                Incident.organisation_id == principal.tenant_id, Incident.service_user_id == su.id
            )
            .order_by(Incident.created_at.desc())
            .limit(10)
        )
    ).scalars()
    profile = ServiceUserProfile(
        id=su.id,
        display_name=su.display_name,
        first_name=su.first_name,
        last_name=su.last_name,
        preferred_name=su.preferred_name,
        phone_number=su.phone_number,
        address_line1=su.address_line1,
        city=su.city,
        postcode=su.postcode,
        external_reference=su.external_reference,
        status=su.status,
        escalation_policy_id=su.escalation_policy_id,
        created_at=su.created_at,
        contacts=[TrustedContactView.model_validate(c) for c in contacts],
        devices=await list_devices(session, principal, low_battery_threshold, su.id),
        recent_incidents=await build_summaries(session, list(incidents)),
    )
    await audit.record_view(
        session,
        principal,
        AuditAction.SERVICE_USER_VIEWED,
        resource_type="service_user",
        resource_id=str(su.id),
        context=context,
    )
    await session.commit()
    return profile


async def create_service_user(
    session: AsyncSession, principal: Principal, body: ServiceUserWrite, context: AuditContext
) -> ServiceUser:
    await _check_policy(session, principal, body.escalation_policy_id)
    su = ServiceUser(organisation_id=principal.tenant_id, **body.model_dump())
    session.add(su)
    await session.flush()
    audit.record(
        session,
        AuditEntry.by(
            principal,
            AuditAction.SERVICE_USER_CREATED,
            resource_type="service_user",
            resource_id=str(su.id),
        ),
        context,
    )
    await session.commit()
    return su


async def update_service_user(
    session: AsyncSession,
    principal: Principal,
    service_user_id: uuid.UUID,
    body: ServiceUserWrite,
    context: AuditContext,
) -> ServiceUser:
    su = await _get(session, principal, service_user_id)
    await _check_policy(session, principal, body.escalation_policy_id)
    changes = body.model_dump()
    changed_fields = sorted(k for k, v in changes.items() if getattr(su, k) != v)
    for key, value in changes.items():
        setattr(su, key, value)
    audit.record(
        session,
        AuditEntry.by(
            principal,
            AuditAction.SERVICE_USER_UPDATED,
            resource_type="service_user",
            resource_id=str(su.id),
            details={"changed_fields": changed_fields},  # field names only, never values
        ),
        context,
    )
    await session.commit()
    return su


async def _ensure_priority_free(
    session: AsyncSession, service_user_id: uuid.UUID, priority: int, exclude: uuid.UUID | None
) -> None:
    stmt = select(TrustedContact.id).where(
        TrustedContact.service_user_id == service_user_id,
        TrustedContact.priority == priority,
        TrustedContact.is_active.is_(True),
        TrustedContact.deleted_at.is_(None),
    )
    if exclude is not None:
        stmt = stmt.where(TrustedContact.id != exclude)
    if await session.scalar(stmt) is not None:
        raise ConflictError("Another active contact already has this priority.")


async def add_contact(
    session: AsyncSession,
    principal: Principal,
    service_user_id: uuid.UUID,
    body: TrustedContactWrite,
    context: AuditContext,
) -> TrustedContactView:
    su = await _get(session, principal, service_user_id)
    if body.is_active:
        await _ensure_priority_free(session, su.id, body.priority, None)
    contact = TrustedContact(
        organisation_id=principal.tenant_id,
        service_user_id=su.id,
        **body.model_dump(mode="json"),
    )
    session.add(contact)
    await session.flush()
    audit.record(
        session,
        AuditEntry.by(
            principal,
            AuditAction.TRUSTED_CONTACT_CREATED,
            resource_type="trusted_contact",
            resource_id=str(contact.id),
            details={"service_user_id": str(su.id), "priority": contact.priority},
        ),
        context,
    )
    await session.commit()
    return TrustedContactView.model_validate(contact)


async def update_contact(
    session: AsyncSession,
    principal: Principal,
    service_user_id: uuid.UUID,
    contact_id: uuid.UUID,
    body: TrustedContactWrite,
    context: AuditContext,
) -> TrustedContactView:
    su = await _get(session, principal, service_user_id)
    contact = await session.scalar(
        select(TrustedContact).where(
            TrustedContact.id == contact_id,
            TrustedContact.service_user_id == su.id,
            TrustedContact.organisation_id == principal.tenant_id,
            TrustedContact.deleted_at.is_(None),
        )
    )
    if contact is None:
        raise NotFoundError()
    if body.is_active:
        await _ensure_priority_free(session, su.id, body.priority, contact.id)
    changes = body.model_dump(mode="json")
    changed_fields = sorted(k for k, v in changes.items() if getattr(contact, k) != v)
    for key, value in changes.items():
        setattr(contact, key, value)
    audit.record(
        session,
        AuditEntry.by(
            principal,
            AuditAction.TRUSTED_CONTACT_UPDATED,
            resource_type="trusted_contact",
            resource_id=str(contact.id),
            details={"service_user_id": str(su.id), "changed_fields": changed_fields},
        ),
        context,
    )
    await session.commit()
    return TrustedContactView.model_validate(contact)
