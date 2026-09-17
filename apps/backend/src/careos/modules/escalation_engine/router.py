from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from careos.api.deps import AuditContextDep, SessionDep, require
from careos.modules.escalation_engine import policies
from careos.modules.escalation_engine.models import EscalationPolicy
from careos.modules.escalation_engine.schemas import EscalationPolicyView, EscalationPolicyWrite
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Permission

router = APIRouter(prefix="/v1/escalation-policies", tags=["escalation policies"])


@router.get("", response_model=list[EscalationPolicyView])
async def list_policies(
    principal: Annotated[Principal, Depends(require(Permission.ESCALATION_POLICIES_READ))],
    session: SessionDep,
) -> list[EscalationPolicy]:
    return await policies.list_policies(session, principal)


@router.post("", response_model=EscalationPolicyView, status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: EscalationPolicyWrite,
    principal: Annotated[Principal, Depends(require(Permission.ESCALATION_POLICIES_MANAGE))],
    session: SessionDep,
    context: AuditContextDep,
) -> EscalationPolicy:
    return await policies.create_policy(session, principal, body, context)


@router.put("/{policy_id}", response_model=EscalationPolicyView)
async def replace_policy(
    policy_id: uuid.UUID,
    body: EscalationPolicyWrite,
    principal: Annotated[Principal, Depends(require(Permission.ESCALATION_POLICIES_MANAGE))],
    session: SessionDep,
    context: AuditContextDep,
) -> EscalationPolicy:
    return await policies.replace_policy(session, principal, policy_id, body, context)
