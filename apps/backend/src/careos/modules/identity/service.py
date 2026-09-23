"""Authentication, sessions and user administration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from careos.core.config import Settings
from careos.core.errors import (
    AuthenticationRequiredError,
    ConflictError,
    InvalidCredentialsError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
)
from careos.core.rate_limit import RateLimiter
from careos.core.security import (
    csrf_token_for_session,
    generate_token,
    hash_password,
    password_needs_rehash,
    session_token_digest,
    verify_password,
)
from careos.core.time import utcnow
from careos.modules.audit import service as audit
from careos.modules.audit.models import AuditActorType, AuditOutcome
from careos.modules.audit.service import AuditAction, AuditContext, AuditEntry
from careos.modules.identity.models import User, UserSession
from careos.modules.identity.principal import Principal
from careos.modules.identity.rbac import ASSIGNABLE_TENANT_ROLES, Role, permissions_for
from careos.modules.organisations.models import Organisation, OrganisationStatus

_SESSION_TOUCH_INTERVAL = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str
    csrf_token: str
    expires_at: datetime
    user: User


def normalise_email(email: str) -> str:
    return email.strip().lower()


class AuthService:
    def __init__(
        self,
        settings: Settings,
        rate_limiter: RateLimiter,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._limiter = rate_limiter
        self._session_factory = session_factory

    @property
    def _secret(self) -> str:
        return self._settings.secret_key.get_secret_value()

    def csrf_token_for(self, token: str) -> str:
        return csrf_token_for_session(self._secret, session_token_digest(self._secret, token))

    async def login(
        self, session: AsyncSession, email: str, password: str, context: AuditContext
    ) -> LoginResult:
        email = normalise_email(email)
        window = self._settings.login_window_seconds
        limit = self._settings.login_max_attempts
        by_account = await self._limiter.hit(f"login:account:{email}", limit, window)
        by_ip = await self._limiter.hit(f"login:ip:{context.ip_address}", limit * 5, window)
        if not (by_account.allowed and by_ip.allowed):
            await self._audit_login_failure(None, "rate_limited", context)
            raise RateLimitedError(max(by_account.retry_after_seconds, by_ip.retry_after_seconds))

        row = (
            await session.execute(
                select(User, Organisation)
                .outerjoin(Organisation, Organisation.id == User.organisation_id)
                .where(User.email == email, User.deleted_at.is_(None))
            )
        ).one_or_none()
        user, organisation = (row[0], row[1]) if row else (None, None)

        # Always run a hash verification so response timing does not reveal account existence.
        password_ok = verify_password(user.password_hash if user else None, password)
        reason: str | None = None
        if user is None or not password_ok:
            reason = "invalid_credentials"
        elif not user.is_active:
            reason = "user_inactive"
        elif user.role != Role.PLATFORM_ADMIN and (
            organisation is None
            or organisation.deleted_at is not None
            or organisation.status != OrganisationStatus.ACTIVE
        ):
            reason = "organisation_inactive"
        elif not permissions_for(user.role):
            reason = "no_console_access"
        if reason is not None or user is None:
            await self._audit_login_failure(user, reason or "invalid_credentials", context)
            raise InvalidCredentialsError()

        now = utcnow()
        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        token = generate_token()
        expires_at = now + timedelta(minutes=self._settings.session_ttl_minutes)
        session.add(
            UserSession(
                user_id=user.id,
                organisation_id=user.organisation_id,
                token_digest=session_token_digest(self._secret, token),
                expires_at=expires_at,
                last_seen_at=now,
                ip_address=context.ip_address,
                user_agent=(context.user_agent or "")[:255] or None,
            )
        )
        user.last_login_at = now
        audit.record(
            session,
            AuditEntry(
                action=AuditAction.AUTH_LOGIN_SUCCEEDED,
                organisation_id=user.organisation_id,
                actor_type=AuditActorType.USER,
                actor_user_id=user.id,
                resource_type="user",
                resource_id=str(user.id),
            ),
            context,
        )
        await session.commit()
        await self._limiter.reset(f"login:account:{email}")
        return LoginResult(
            token=token, csrf_token=self.csrf_token_for(token), expires_at=expires_at, user=user
        )

    async def _audit_login_failure(
        self, user: User | None, reason: str, context: AuditContext
    ) -> None:
        await audit.record_isolated(
            self._session_factory,
            AuditEntry(
                action=AuditAction.AUTH_LOGIN_FAILED,
                outcome=AuditOutcome.FAILURE,
                organisation_id=user.organisation_id if user else None,
                actor_type=AuditActorType.USER if user else AuditActorType.ANONYMOUS,
                actor_user_id=user.id if user else None,
                details={"reason": reason},
            ),
            context,
        )

    async def authenticate(self, session: AsyncSession, token: str | None) -> Principal:
        if not token or len(token) > 256:
            raise AuthenticationRequiredError()
        now = utcnow()
        row = (
            await session.execute(
                select(UserSession, User)
                .join(User, User.id == UserSession.user_id)
                .outerjoin(Organisation, Organisation.id == User.organisation_id)
                .where(
                    UserSession.token_digest == session_token_digest(self._secret, token),
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > now,
                    User.deleted_at.is_(None),
                    User.is_active.is_(True),
                    # A suspended or deleted tenant loses access immediately, not at next login.
                    or_(
                        User.organisation_id.is_(None),
                        and_(
                            Organisation.status == OrganisationStatus.ACTIVE,
                            Organisation.deleted_at.is_(None),
                        ),
                    ),
                )
            )
        ).one_or_none()
        if row is None:
            raise AuthenticationRequiredError()
        user_session, user = row
        idle_limit = timedelta(minutes=self._settings.session_idle_timeout_minutes)
        if now - user_session.last_seen_at > idle_limit:
            user_session.revoked_at = now
            await session.commit()
            raise AuthenticationRequiredError("Session expired due to inactivity.")
        if now - user_session.last_seen_at > _SESSION_TOUCH_INTERVAL:
            user_session.last_seen_at = now
            await session.commit()
        return Principal(
            user_id=user.id,
            organisation_id=user.organisation_id,
            role=user.role,
            session_id=user_session.id,
            full_name=user.full_name,
        )

    async def logout(
        self, session: AsyncSession, principal: Principal, context: AuditContext
    ) -> None:
        await session.execute(
            update(UserSession)
            .where(UserSession.id == principal.session_id)
            .values(revoked_at=utcnow())
        )
        audit.record(session, AuditEntry.by(principal, AuditAction.AUTH_LOGOUT), context)
        await session.commit()


class UserAdminService:
    """Tenant user management. All queries are scoped to ``principal.tenant_id``."""

    async def list_users(self, session: AsyncSession, principal: Principal) -> list[User]:
        result = await session.execute(
            select(User)
            .where(User.organisation_id == principal.tenant_id, User.deleted_at.is_(None))
            .order_by(User.full_name)
        )
        return list(result.scalars())

    async def create_user(
        self,
        session: AsyncSession,
        principal: Principal,
        *,
        email: str,
        full_name: str,
        role: Role,
        password: str,
        context: AuditContext,
    ) -> User:
        if role not in ASSIGNABLE_TENANT_ROLES:
            raise PermissionDeniedError("This role cannot be assigned.")
        email = normalise_email(email)
        exists = await session.scalar(
            select(User.id).where(User.email == email, User.deleted_at.is_(None))
        )
        if exists:
            raise ConflictError("A user with this email already exists.")
        user = User(
            organisation_id=principal.tenant_id,
            email=email,
            full_name=full_name,
            role=role,
            password_hash=hash_password(password),
        )
        session.add(user)
        await session.flush()
        audit.record(
            session,
            AuditEntry.by(
                principal,
                AuditAction.USER_CREATED,
                resource_type="user",
                resource_id=str(user.id),
                details={"role": role.value},
            ),
            context,
        )
        await session.commit()
        return user

    async def change_role(
        self,
        session: AsyncSession,
        principal: Principal,
        user_id: uuid.UUID,
        role: Role,
        context: AuditContext,
    ) -> User:
        if role not in ASSIGNABLE_TENANT_ROLES:
            raise PermissionDeniedError("This role cannot be assigned.")
        if user_id == principal.user_id:
            raise ConflictError("You cannot change your own role.")
        user = await session.scalar(
            select(User)
            .where(
                User.id == user_id,
                User.organisation_id == principal.tenant_id,
                User.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if user is None:
            raise NotFoundError()
        previous = user.role
        user.role = role
        # Permissions changed: force the user to sign in again.
        await session.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        audit.record(
            session,
            AuditEntry.by(
                principal,
                AuditAction.USER_PERMISSIONS_CHANGED,
                resource_type="user",
                resource_id=str(user.id),
                details={"from_role": previous.value, "to_role": role.value},
            ),
            context,
        )
        await session.commit()
        return user
