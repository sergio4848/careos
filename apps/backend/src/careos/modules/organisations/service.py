from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careos.core.errors import ConflictError, NotFoundError
from careos.core.security import hash_password
from careos.modules.audit import service as audit
from careos.modules.audit.service import AuditAction, AuditContext, AuditEntry
from careos.modules.escalation_engine.policies import create_default_policy
from careos.modules.identity.models import User
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import Role
from careos.modules.identity.service import normalise_email
from careos.modules.organisations.models import Organisation
from careos.modules.organisations.schemas import CreateOrganisationRequest


async def get_current(session: AsyncSession, principal: Principal) -> Organisation:
    organisation = await session.get(Organisation, principal.tenant_id)
    if organisation is None or organisation.deleted_at is not None:
        raise NotFoundError()
    return organisation


async def list_all(session: AsyncSession) -> list[Organisation]:
    result = await session.execute(
        select(Organisation).where(Organisation.deleted_at.is_(None)).order_by(Organisation.name)
    )
    return list(result.scalars())


async def onboard(
    session: AsyncSession,
    principal: Principal,
    request: CreateOrganisationRequest,
    context: AuditContext,
) -> Organisation:
    """Create a tenant with a safe default escalation policy and its first administrator."""
    if await session.scalar(select(Organisation.id).where(Organisation.slug == request.slug)):
        raise ConflictError("An organisation with this slug already exists.")
    email = normalise_email(str(request.admin_email))
    if await session.scalar(select(User.id).where(User.email == email, User.deleted_at.is_(None))):
        raise ConflictError("A user with this email already exists.")

    organisation = Organisation(
        name=request.name,
        slug=request.slug,
        country_code=request.country_code,
        timezone=request.timezone,
    )
    session.add(organisation)
    await session.flush()
    session.add(
        User(
            organisation_id=organisation.id,
            email=email,
            full_name=request.admin_full_name,
            role=Role.ORGANISATION_ADMIN,
            password_hash=hash_password(request.admin_password),
        )
    )
    await create_default_policy(session, organisation.id)
    audit.record(
        session,
        AuditEntry.by(
            principal,
            AuditAction.ORGANISATION_CREATED,
            resource_type="organisation",
            resource_id=str(organisation.id),
            details={"slug": organisation.slug},
        ),
        context,
    )
    await session.commit()
    return organisation
