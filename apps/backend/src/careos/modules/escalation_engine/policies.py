"""Escalation policy configuration (per organisation) and its safety invariants."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from careos.core.errors import NotFoundError, ValidationFailedError
from careos.modules.audit import service as audit
from careos.modules.audit.service import AuditAction, AuditContext, AuditEntry
from careos.modules.escalation_engine.models import (
    CONTACT_ACTIONS,
    EscalationActionType,
    EscalationPolicy,
    EscalationStep,
)
from careos.modules.escalation_engine.schemas import EscalationPolicyWrite, EscalationStepWrite
from careos.modules.identity.principal import Principal
from careos.modules.service_users.models import ServiceUser

MAX_STEPS = 20
MAX_DELAY_SECONDS = 24 * 3600


@dataclass(frozen=True, slots=True)
class StepSpec:
    step_order: int
    delay_seconds: int
    action_type: EscalationActionType
    contact_priority: int | None = None
    escalation_step_id: uuid.UUID | None = None


DEFAULT_POLICY_NAME = "Standard SOS escalation"
DEFAULT_STEPS: tuple[StepSpec, ...] = (
    StepSpec(1, 0, EscalationActionType.AUTOMATED_USER_CONTACT),
    StepSpec(2, 30, EscalationActionType.CALL_TRUSTED_CONTACT, contact_priority=1),
    StepSpec(3, 60, EscalationActionType.CALL_TRUSTED_CONTACT, contact_priority=2),
    StepSpec(4, 90, EscalationActionType.OPERATOR_ESCALATION),
)

#: Used when no policy applies (unassigned device, device fault, missing configuration).
#: Fail safe: a human operator is alerted immediately.
FALLBACK_STEPS: tuple[StepSpec, ...] = (StepSpec(1, 0, EscalationActionType.OPERATOR_ESCALATION),)


def validate_steps(steps: list[EscalationStepWrite]) -> None:
    """Invariants that keep a misconfigured policy from silently dropping an alarm."""
    if not steps:
        raise ValidationFailedError("A policy needs at least one step.")
    if len(steps) > MAX_STEPS:
        raise ValidationFailedError(f"A policy may have at most {MAX_STEPS} steps.")
    delays = [s.delay_seconds for s in steps]
    if delays != sorted(delays):
        raise ValidationFailedError("Step delays must be in non-decreasing order.")
    if any(d > MAX_DELAY_SECONDS for d in delays):
        raise ValidationFailedError("Step delays must be at most 24 hours.")
    if not any(s.action_type == EscalationActionType.OPERATOR_ESCALATION for s in steps):
        raise ValidationFailedError("Every policy must include an OPERATOR_ESCALATION step.")
    for step in steps:
        if step.action_type in CONTACT_ACTIONS and step.contact_priority is None:
            raise ValidationFailedError("Trusted contact steps require a contact_priority.")


async def create_default_policy(
    session: AsyncSession, organisation_id: uuid.UUID
) -> EscalationPolicy:
    policy = EscalationPolicy(
        organisation_id=organisation_id,
        name=DEFAULT_POLICY_NAME,
        description="T+0 automated welfare call, T+30s trusted contact #1, "
        "T+60s trusted contact #2, T+90s alert operators.",
        is_default=True,
        steps=[
            EscalationStep(
                organisation_id=organisation_id,
                step_order=spec.step_order,
                delay_seconds=spec.delay_seconds,
                action_type=spec.action_type,
                contact_priority=spec.contact_priority,
            )
            for spec in DEFAULT_STEPS
        ],
    )
    session.add(policy)
    await session.flush()
    return policy


async def resolve_policy(
    session: AsyncSession, organisation_id: uuid.UUID, service_user: ServiceUser | None
) -> EscalationPolicy | None:
    """Service-user override first, then the organisation default. Inactive policies are ignored."""
    base = (
        select(EscalationPolicy)
        .options(selectinload(EscalationPolicy.steps))
        .where(
            EscalationPolicy.organisation_id == organisation_id,
            EscalationPolicy.deleted_at.is_(None),
            EscalationPolicy.is_active.is_(True),
        )
    )
    if service_user is not None and service_user.escalation_policy_id is not None:
        policy = await session.scalar(
            base.where(EscalationPolicy.id == service_user.escalation_policy_id)
        )
        if policy is not None and policy.steps:
            return policy
    policy = await session.scalar(base.where(EscalationPolicy.is_default.is_(True)))
    return policy if policy is not None and policy.steps else None


def steps_of(policy: EscalationPolicy) -> list[StepSpec]:
    return [
        StepSpec(
            step_order=step.step_order,
            delay_seconds=step.delay_seconds,
            action_type=step.action_type,
            contact_priority=step.contact_priority,
            escalation_step_id=step.id,
        )
        for step in policy.steps
    ]


# --------------------------------------------------------------------------- management


async def list_policies(session: AsyncSession, principal: Principal) -> list[EscalationPolicy]:
    result = await session.execute(
        select(EscalationPolicy)
        .options(selectinload(EscalationPolicy.steps))
        .where(
            EscalationPolicy.organisation_id == principal.tenant_id,
            EscalationPolicy.deleted_at.is_(None),
        )
        .order_by(EscalationPolicy.is_default.desc(), EscalationPolicy.name)
    )
    return list(result.scalars())


def _summary(steps: list[EscalationStepWrite] | list[EscalationStep]) -> list[dict[str, object]]:
    return [
        {
            "delay_seconds": s.delay_seconds,
            "action_type": s.action_type.value,
            "contact_priority": s.contact_priority,
        }
        for s in steps
    ]


async def _clear_other_defaults(
    session: AsyncSession, organisation_id: uuid.UUID, keep_id: uuid.UUID | None
) -> None:
    stmt = update(EscalationPolicy).where(
        EscalationPolicy.organisation_id == organisation_id,
        EscalationPolicy.is_default.is_(True),
    )
    if keep_id is not None:
        stmt = stmt.where(EscalationPolicy.id != keep_id)
    await session.execute(stmt.values(is_default=False))


async def create_policy(
    session: AsyncSession, principal: Principal, body: EscalationPolicyWrite, context: AuditContext
) -> EscalationPolicy:
    validate_steps(body.steps)
    org = principal.tenant_id
    if body.is_default:
        await _clear_other_defaults(session, org, None)
    policy = EscalationPolicy(
        organisation_id=org,
        name=body.name,
        description=body.description,
        is_default=body.is_default,
        is_active=body.is_active,
        steps=[
            EscalationStep(
                organisation_id=org,
                step_order=index,
                delay_seconds=s.delay_seconds,
                action_type=s.action_type,
                contact_priority=s.contact_priority,
            )
            for index, s in enumerate(body.steps, start=1)
        ],
    )
    session.add(policy)
    await session.flush()
    audit.record(
        session,
        AuditEntry.by(
            principal,
            AuditAction.ESCALATION_POLICY_CREATED,
            resource_type="escalation_policy",
            resource_id=str(policy.id),
            details={"steps": _summary(body.steps), "is_default": body.is_default},
        ),
        context,
    )
    await session.commit()
    return await get_policy(session, principal, policy.id)


async def get_policy(
    session: AsyncSession, principal: Principal, policy_id: uuid.UUID
) -> EscalationPolicy:
    policy = await session.scalar(
        select(EscalationPolicy)
        .options(selectinload(EscalationPolicy.steps))
        .where(
            EscalationPolicy.id == policy_id,
            EscalationPolicy.organisation_id == principal.tenant_id,
            EscalationPolicy.deleted_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    if policy is None:
        raise NotFoundError()
    return policy


async def replace_policy(
    session: AsyncSession,
    principal: Principal,
    policy_id: uuid.UUID,
    body: EscalationPolicyWrite,
    context: AuditContext,
) -> EscalationPolicy:
    """Replace a policy's steps. Running incidents keep their snapshot (ADR-007)."""
    validate_steps(body.steps)
    policy = await get_policy(session, principal, policy_id)
    before = _summary(policy.steps)
    if body.is_default and not policy.is_default:
        await _clear_other_defaults(session, principal.tenant_id, policy.id)
    policy.name = body.name
    policy.description = body.description
    policy.is_default = body.is_default
    policy.is_active = body.is_active
    policy.revision += 1
    policy.steps.clear()
    await session.flush()
    policy.steps.extend(
        EscalationStep(
            organisation_id=policy.organisation_id,
            step_order=index,
            delay_seconds=s.delay_seconds,
            action_type=s.action_type,
            contact_priority=s.contact_priority,
        )
        for index, s in enumerate(body.steps, start=1)
    )
    audit.record(
        session,
        AuditEntry.by(
            principal,
            AuditAction.ESCALATION_POLICY_CHANGED,
            resource_type="escalation_policy",
            resource_id=str(policy.id),
            details={"before": before, "after": _summary(body.steps), "revision": policy.revision},
        ),
        context,
    )
    await session.commit()
    return await get_policy(session, principal, policy.id)
