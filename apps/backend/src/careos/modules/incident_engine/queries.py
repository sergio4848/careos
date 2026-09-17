"""Read models for the operator console. Every query is scoped by ``principal.tenant_id``."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careos.core.errors import NotFoundError
from careos.modules.devices.models import Device, DeviceConnection
from careos.modules.escalation_engine.models import (
    OPEN_ACTION_STATUSES,
    ScheduledAction,
)
from careos.modules.escalation_engine.scheduler import describe_action
from careos.modules.escalation_engine.schemas import ScheduledActionView
from careos.modules.identity.models import User
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission
from careos.modules.incident_engine.models import Incident, IncidentEvent
from careos.modules.incident_engine.schemas import (
    DeviceBrief,
    IncidentDetail,
    IncidentEventView,
    IncidentSummary,
    LocationView,
    NextActionView,
    PersonRef,
    ServiceUserBrief,
)
from careos.modules.incident_engine.state_machine import (
    ACTIVE_STATUSES,
    AWAITING_CLOSURE_STATUSES,
    TAKEOVER_STATUSES,
)
from careos.modules.service_users.models import ServiceUser, TrustedContact

IncidentScope = Literal["active", "awaiting_closure", "recent"]


async def _people(session: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, PersonRef]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.execute(select(User.id, User.full_name).where(User.id.in_(wanted)))
    return {row.id: PersonRef(id=row.id, name=row.full_name) for row in rows}


async def _contact_names(
    session: AsyncSession, service_user_ids: set[uuid.UUID]
) -> dict[tuple[uuid.UUID, int], str]:
    if not service_user_ids:
        return {}
    rows = await session.execute(
        select(
            TrustedContact.service_user_id, TrustedContact.priority, TrustedContact.full_name
        ).where(
            TrustedContact.service_user_id.in_(service_user_ids),
            TrustedContact.deleted_at.is_(None),
            TrustedContact.is_active.is_(True),
        )
    )
    return {(row.service_user_id, row.priority): row.full_name for row in rows}


async def build_summaries(
    session: AsyncSession, incidents: Sequence[Incident]
) -> list[IncidentSummary]:
    if not incidents:
        return []
    incident_ids = [i.id for i in incidents]
    su_ids = {i.service_user_id for i in incidents if i.service_user_id}
    device_ids = {i.device_id for i in incidents if i.device_id}

    service_users = (
        {
            su.id: su
            for su in (
                await session.execute(select(ServiceUser).where(ServiceUser.id.in_(su_ids)))
            ).scalars()
        }
        if su_ids
        else {}
    )
    devices: dict[uuid.UUID, tuple[Device, DeviceConnection | None]] = {}
    if device_ids:
        rows = await session.execute(
            select(Device, DeviceConnection)
            .outerjoin(DeviceConnection, DeviceConnection.device_id == Device.id)
            .where(Device.id.in_(device_ids))
        )
        devices = {row[0].id: (row[0], row[1]) for row in rows}

    next_actions: dict[uuid.UUID, ScheduledAction] = {}
    rows_na = await session.execute(
        select(ScheduledAction)
        .where(
            ScheduledAction.incident_id.in_(incident_ids),
            ScheduledAction.status.in_(OPEN_ACTION_STATUSES),
        )
        .order_by(ScheduledAction.incident_id, ScheduledAction.due_at, ScheduledAction.step_order)
        .distinct(ScheduledAction.incident_id)
    )
    for pending in rows_na.scalars():
        next_actions[pending.incident_id] = pending

    people = await _people(session, {i.assigned_user_id for i in incidents})
    contacts = await _contact_names(session, su_ids)

    summaries: list[IncidentSummary] = []
    for incident in incidents:
        su = service_users.get(incident.service_user_id) if incident.service_user_id else None
        device_row = devices.get(incident.device_id) if incident.device_id else None
        action = next_actions.get(incident.id)
        summaries.append(
            IncidentSummary(
                id=incident.id,
                reference=incident.reference,
                status=incident.status,
                priority=incident.priority,
                trigger_type=incident.trigger_type,
                is_active=incident.is_active,
                created_at=incident.created_at,
                updated_at=incident.updated_at,
                acknowledged_at=incident.acknowledged_at,
                resolved_at=incident.resolved_at,
                closed_at=incident.closed_at,
                service_user=(
                    ServiceUserBrief(id=su.id, display_name=su.display_name, city=su.city)
                    if su
                    else None
                ),
                device=(
                    DeviceBrief(
                        id=device_row[0].id,
                        external_id=device_row[0].external_id,
                        device_type=device_row[0].device_type.value,
                        battery_level=device_row[1].battery_level if device_row[1] else None,
                        signal_strength=device_row[1].signal_strength if device_row[1] else None,
                    )
                    if device_row
                    else None
                ),
                assignee=people.get(incident.assigned_user_id)
                if incident.assigned_user_id
                else None,
                next_action=(
                    NextActionView(
                        step_order=action.step_order,
                        action_type=action.action_type.value,
                        label=describe_action(
                            action.action_type,
                            action.contact_priority,
                            contact_name=(
                                contacts.get((su.id, action.contact_priority))
                                if su and action.contact_priority
                                else None
                            ),
                            service_user_name=su.display_name if su else None,
                        ),
                        due_at=action.due_at,
                    )
                    if action
                    else None
                ),
            )
        )
    return summaries


async def list_incidents(
    session: AsyncSession, principal: Principal, scope: IncidentScope, limit: int
) -> list[IncidentSummary]:
    stmt = select(Incident).where(Incident.organisation_id == principal.tenant_id)
    if scope == "active":
        stmt = stmt.where(Incident.status.in_(ACTIVE_STATUSES)).order_by(Incident.created_at)
    elif scope == "awaiting_closure":
        stmt = stmt.where(Incident.status.in_(AWAITING_CLOSURE_STATUSES)).order_by(
            Incident.resolved_at.desc()
        )
    else:
        stmt = stmt.order_by(Incident.created_at.desc())
    incidents = list((await session.execute(stmt.limit(limit))).scalars())
    summaries = await build_summaries(session, incidents)
    if scope == "active":
        summaries.sort(key=lambda s: (s.priority.rank, s.created_at))
    return summaries


async def _get(session: AsyncSession, principal: Principal, incident_id: uuid.UUID) -> Incident:
    incident = await session.scalar(
        select(Incident).where(
            Incident.id == incident_id, Incident.organisation_id == principal.tenant_id
        )
    )
    if incident is None:
        raise NotFoundError()
    return incident


def allowed_actions(principal: Principal, incident: Incident) -> list[str]:
    actions: list[str] = []
    owned_by_other = (
        incident.assigned_user_id is not None and incident.assigned_user_id != principal.user_id
    )
    can_override = principal.has(Permission.INCIDENTS_OVERRIDE_ASSIGNMENT)
    if (
        principal.has(Permission.INCIDENTS_TAKEOVER)
        and incident.status in TAKEOVER_STATUSES
        and incident.assigned_user_id is None
    ):
        actions.append("takeover")
    if (
        principal.has(Permission.INCIDENTS_RESOLVE)
        and incident.status in ACTIVE_STATUSES
        and (not owned_by_other or can_override)
    ):
        actions.append("resolve")
    if principal.has(Permission.INCIDENTS_CLOSE) and incident.status in AWAITING_CLOSURE_STATUSES:
        actions.append("close")
    return actions


async def get_detail(
    session: AsyncSession, principal: Principal, incident_id: uuid.UUID
) -> IncidentDetail:
    incident = await _get(session, principal, incident_id)
    (summary,) = await build_summaries(session, [incident])
    actions = list(
        (
            await session.execute(
                select(ScheduledAction)
                .where(ScheduledAction.incident_id == incident.id)
                .order_by(ScheduledAction.due_at, ScheduledAction.step_order)
            )
        ).scalars()
    )
    su_name = summary.service_user.display_name if summary.service_user else None
    contacts = (
        await _contact_names(session, {incident.service_user_id})
        if incident.service_user_id
        else {}
    )
    people = await _people(session, {incident.resolved_by_user_id, incident.closed_by_user_id})
    return IncidentDetail(
        **summary.model_dump(),
        location=(
            LocationView(latitude=incident.latitude, longitude=incident.longitude)
            if incident.latitude is not None and incident.longitude is not None
            else None
        ),
        resolution_category=incident.resolution_category,
        resolution_notes=incident.resolution_notes,
        resolved_by=people.get(incident.resolved_by_user_id)
        if incident.resolved_by_user_id
        else None,
        closed_by=people.get(incident.closed_by_user_id) if incident.closed_by_user_id else None,
        escalation=[
            ScheduledActionView(
                id=a.id,
                step_order=a.step_order,
                action_type=a.action_type,
                label=describe_action(
                    a.action_type,
                    a.contact_priority,
                    contact_name=(
                        contacts.get((incident.service_user_id, a.contact_priority))
                        if incident.service_user_id and a.contact_priority
                        else None
                    ),
                    service_user_name=su_name,
                ),
                delay_seconds=a.delay_seconds,
                due_at=a.due_at,
                status=a.status,
                attempts=a.attempts,
                completed_at=a.completed_at,
            )
            for a in actions
        ],
        allowed_actions=allowed_actions(principal, incident),
    )


async def get_timeline(
    session: AsyncSession, principal: Principal, incident_id: uuid.UUID
) -> list[IncidentEventView]:
    incident = await _get(session, principal, incident_id)
    events = list(
        (
            await session.execute(
                select(IncidentEvent)
                .where(IncidentEvent.incident_id == incident.id)
                .order_by(IncidentEvent.sequence)
            )
        ).scalars()
    )
    people = await _people(session, {e.actor_user_id for e in events})
    return [
        IncidentEventView(
            id=e.id,
            sequence=e.sequence,
            event_type=e.event_type,
            actor_type=e.actor_type,
            actor=people.get(e.actor_user_id) if e.actor_user_id else None,
            from_status=e.from_status,
            to_status=e.to_status,
            message=e.message,
            data=e.data,
            occurred_at=e.occurred_at,
            created_at=e.created_at,
        )
        for e in events
    ]
