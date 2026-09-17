from __future__ import annotations

from careos.bootstrap import Container
from careos.modules.identity.rbac import Role
from tests.factories import ClientFactory, Tenant
from tests.integration.helpers import audit_actions


async def test_login_sets_httponly_session_cookie_and_returns_csrf(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    client = await client_factory()
    response = await client.post(
        "/v1/auth/login",
        json={"email": tenant.emails[Role.OPERATOR].upper(), "password": "Correct-Horse-Battery-9"},
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "careos_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    body = response.json()
    assert body["user"]["role"] == "OPERATOR"
    assert "incidents:takeover" in body["permissions"]
    assert body["csrf_token"]
    assert "password_hash" not in response.text
    assert "AUTH_LOGIN_SUCCEEDED" in await audit_actions(container.session_factory)


async def test_wrong_password_is_rejected_and_audited(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    client = await client_factory()
    response = await client.post(
        "/v1/auth/login", json={"email": tenant.emails[Role.OPERATOR], "password": "nope"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    unknown = await client.post(
        "/v1/auth/login", json={"email": "nobody@example.com", "password": "nope"}
    )
    assert unknown.json()["error"]["message"] == response.json()["error"]["message"]
    assert (await audit_actions(container.session_factory)).count("AUTH_LOGIN_FAILED") == 2


async def test_login_is_rate_limited(client_factory: ClientFactory, tenant: Tenant) -> None:
    client = await client_factory()
    statuses = [
        (
            await client.post(
                "/v1/auth/login", json={"email": tenant.emails[Role.OPERATOR], "password": "bad"}
            )
        ).status_code
        for _ in range(6)
    ]
    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429


async def test_trusted_contact_role_has_no_console_access(
    client_factory: ClientFactory, tenant: Tenant
) -> None:
    client = await client_factory()
    response = await client.post(
        "/v1/auth/login",
        json={"email": tenant.emails[Role.TRUSTED_CONTACT], "password": "Correct-Horse-Battery-9"},
    )
    assert response.status_code == 401


async def test_unauthenticated_requests_are_rejected(client_factory: ClientFactory) -> None:
    client = await client_factory()
    for path in ("/v1/auth/me", "/v1/incidents", "/v1/dashboard/summary", "/v1/devices"):
        response = await client.get(path)
        assert response.status_code == 401, path


async def test_state_changes_require_csrf_token(
    client_factory: ClientFactory, tenant: Tenant
) -> None:
    client = await client_factory(tenant.emails[Role.OPERATOR])
    token = client.headers.pop("X-CSRF-Token")
    response = await client.post("/v1/auth/logout")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    client.headers["X-CSRF-Token"] = "forged"
    assert (await client.post("/v1/auth/logout")).status_code == 403
    client.headers["X-CSRF-Token"] = token
    assert (await client.post("/v1/auth/logout")).status_code == 204


async def test_logout_revokes_the_session(client_factory: ClientFactory, tenant: Tenant) -> None:
    client = await client_factory(tenant.emails[Role.OPERATOR])
    session_cookie = client.cookies.get("careos_session")
    assert (await client.post("/v1/auth/logout")).status_code == 204
    replay = await client_factory()
    replay.cookies.set("careos_session", session_cookie)
    assert (await replay.get("/v1/auth/me")).status_code == 401


async def test_role_change_is_audited_and_revokes_sessions(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    admin = await client_factory(tenant.emails[Role.ORGANISATION_ADMIN])
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    users = (await admin.get("/v1/users")).json()
    operator_id = next(u["id"] for u in users if u["email"] == tenant.emails[Role.OPERATOR])

    response = await admin.patch(f"/v1/users/{operator_id}/role", json={"role": "CAREGIVER"})
    assert response.status_code == 200 and response.json()["role"] == "CAREGIVER"
    assert (await operator.get("/v1/auth/me")).status_code == 401
    assert "USER_PERMISSIONS_CHANGED" in await audit_actions(container.session_factory)

    escalate = await admin.patch(f"/v1/users/{operator_id}/role", json={"role": "PLATFORM_ADMIN"})
    assert escalate.status_code == 403


async def test_operator_cannot_manage_users_and_denial_is_audited(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    operator = await client_factory(tenant.emails[Role.OPERATOR])
    response = await operator.post(
        "/v1/users",
        json={
            "email": "new@example.com",
            "full_name": "New",
            "role": "ORGANISATION_ADMIN",
            "password": "a-long-password-123",
        },
    )
    assert response.status_code == 403
    assert "ACCESS_DENIED" in await audit_actions(container.session_factory)
