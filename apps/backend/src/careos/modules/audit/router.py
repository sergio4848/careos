from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from careos.api.deps import SessionDep, require
from careos.modules.audit.models import AuditActorType, AuditLog, AuditOutcome
from careos.modules.identity.models import User
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission

router = APIRouter(prefix="/v1/audit-logs", tags=["audit"])


class AuditLogView(BaseModel):
    id: uuid.UUID
    created_at: datetime
    action: str
    outcome: AuditOutcome
    actor_type: AuditActorType
    actor_name: str | None
    resource_type: str | None
    resource_id: str | None
    request_id: str | None
    details: dict[str, Any]


@router.get(
    "",
    response_model=list[AuditLogView],
    description="Read-only. Audit records cannot be modified.",
)
async def list_audit_logs(
    principal: Annotated[Principal, Depends(require(Permission.AUDIT_READ))],
    session: SessionDep,
    resource_type: Annotated[str | None, Query(max_length=40)] = None,
    resource_id: Annotated[str | None, Query(max_length=64)] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditLogView]:
    stmt = (
        select(AuditLog, User.full_name)
        .outerjoin(User, User.id == AuditLog.actor_user_id)
        .where(AuditLog.organisation_id == principal.tenant_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    rows = await session.execute(stmt)
    return [
        AuditLogView(
            id=log.id,
            created_at=log.created_at,
            action=log.action,
            outcome=log.outcome,
            actor_type=log.actor_type,
            actor_name=name,
            resource_type=log.resource_type,
            resource_id=log.resource_id,
            request_id=log.request_id,
            details=log.details,
        )
        for log, name in rows.tuples()
    ]
