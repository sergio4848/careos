from __future__ import annotations

import uuid
from dataclasses import dataclass

from careos.core.errors import PermissionDeniedError
from careos.modules.identity.rbac import Permission, Role, permissions_for


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated actor for a request. The ONLY source of tenant identity."""

    user_id: uuid.UUID
    organisation_id: uuid.UUID | None
    role: Role
    session_id: uuid.UUID
    full_name: str

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.role)

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    @property
    def tenant_id(self) -> uuid.UUID:
        """Organisation scope for tenant data. Platform admins have none, by design."""
        if self.organisation_id is None:
            raise PermissionDeniedError("This action requires an organisation context.")
        return self.organisation_id
