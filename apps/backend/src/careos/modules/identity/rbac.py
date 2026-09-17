"""Role-based access control.

Permissions are the unit of authorisation; roles are named bundles of permissions.
Endpoints depend on *permissions*, never on role names, so custom roles (stored in the
database per organisation) can be introduced later without touching route code.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    PLATFORM_ADMIN = "PLATFORM_ADMIN"
    ORGANISATION_ADMIN = "ORGANISATION_ADMIN"
    CARE_MANAGER = "CARE_MANAGER"
    OPERATOR = "OPERATOR"
    CAREGIVER = "CAREGIVER"
    TRUSTED_CONTACT = "TRUSTED_CONTACT"


class Permission(StrEnum):
    PLATFORM_ORGANISATIONS_MANAGE = "platform:organisations:manage"
    ORGANISATION_READ = "organisation:read"
    USERS_READ = "users:read"
    USERS_MANAGE = "users:manage"
    SERVICE_USERS_READ = "service_users:read"
    SERVICE_USERS_MANAGE = "service_users:manage"
    DEVICES_READ = "devices:read"
    DEVICES_MANAGE = "devices:manage"
    INCIDENTS_READ = "incidents:read"
    INCIDENTS_TAKEOVER = "incidents:takeover"
    INCIDENTS_RESOLVE = "incidents:resolve"
    INCIDENTS_CLOSE = "incidents:close"
    INCIDENTS_OVERRIDE_ASSIGNMENT = "incidents:override_assignment"
    ESCALATION_POLICIES_READ = "escalation_policies:read"
    ESCALATION_POLICIES_MANAGE = "escalation_policies:manage"
    AUDIT_READ = "audit:read"
    DASHBOARD_READ = "dashboard:read"
    SIMULATOR_USE = "simulator:use"


_OPERATOR_PERMISSIONS = frozenset(
    {
        Permission.ORGANISATION_READ,
        Permission.DASHBOARD_READ,
        Permission.SERVICE_USERS_READ,
        Permission.DEVICES_READ,
        Permission.INCIDENTS_READ,
        Permission.INCIDENTS_TAKEOVER,
        Permission.INCIDENTS_RESOLVE,
        Permission.INCIDENTS_CLOSE,
        Permission.ESCALATION_POLICIES_READ,
        Permission.SIMULATOR_USE,
    }
)

_CARE_MANAGER_PERMISSIONS = _OPERATOR_PERMISSIONS | {
    Permission.USERS_READ,
    Permission.SERVICE_USERS_MANAGE,
    Permission.DEVICES_MANAGE,
    Permission.INCIDENTS_OVERRIDE_ASSIGNMENT,
    Permission.ESCALATION_POLICIES_MANAGE,
    Permission.AUDIT_READ,
}

_ORGANISATION_ADMIN_PERMISSIONS = _CARE_MANAGER_PERMISSIONS | {Permission.USERS_MANAGE}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    # Least privilege: platform admins manage tenants but cannot browse tenant care data.
    Role.PLATFORM_ADMIN: frozenset({Permission.PLATFORM_ORGANISATIONS_MANAGE}),
    Role.ORGANISATION_ADMIN: frozenset(_ORGANISATION_ADMIN_PERMISSIONS),
    Role.CARE_MANAGER: frozenset(_CARE_MANAGER_PERMISSIONS),
    Role.OPERATOR: _OPERATOR_PERMISSIONS,
    Role.CAREGIVER: frozenset(
        {
            Permission.ORGANISATION_READ,
            Permission.SERVICE_USERS_READ,
            Permission.INCIDENTS_READ,
        }
    ),
    # Future family portal; no console access in the MVP.
    Role.TRUSTED_CONTACT: frozenset(),
}

# Roles an organisation admin may grant. Nobody can grant PLATFORM_ADMIN through the tenant API.
ASSIGNABLE_TENANT_ROLES = frozenset(
    {
        Role.ORGANISATION_ADMIN,
        Role.CARE_MANAGER,
        Role.OPERATOR,
        Role.CAREGIVER,
        Role.TRUSTED_CONTACT,
    }
)


def permissions_for(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())
