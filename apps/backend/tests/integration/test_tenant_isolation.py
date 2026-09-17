"""Cross-tenant data leakage tests (mandatory).

Organisation B's staff must not be able to read, infer or change organisation A's data
through any endpoint. Cross-tenant lookups return 404 (not 403) so existence is not leaked.
"""

from __future__ import annotations

from careos.bootstrap import Container
from careos.contracts.realtime import RealtimeMessage, RealtimeMessageType
from careos.modules.identity.rbac import Role
from careos.modules.realtime.hub import ConnectionHub
from tests.factories import ClientFactory, Tenant
from tests.integration.helpers import incident_count, raise_sos


async def test_other_organisation_cannot_read_or_act_on_incidents(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant, container: Container
) -> None:
    gateway = await client_factory()
    incident_id = (await raise_sos(gateway, tenant))["incident_id"]
    intruder = await client_factory(other_tenant.emails[Role.ORGANISATION_ADMIN])

    assert (await intruder.get("/v1/incidents")).json() == []
    assert (await intruder.get("/v1/incidents", params={"scope": "recent"})).json() == []
    for method, path, payload in (
        ("GET", f"/v1/incidents/{incident_id}", None),
        ("GET", f"/v1/incidents/{incident_id}/timeline", None),
        ("POST", f"/v1/incidents/{incident_id}/takeover", None),
        (
            "POST",
            f"/v1/incidents/{incident_id}/resolve",
            {"category": "USER_SAFE", "notes": "hijack"},
        ),
        ("POST", f"/v1/incidents/{incident_id}/close", {}),
    ):
        response = await intruder.request(method, path, json=payload)
        assert response.status_code == 404, (method, path, response.text)
        assert "Margaret" not in response.text

    owner = await client_factory(tenant.emails[Role.OPERATOR])
    detail = (await owner.get(f"/v1/incidents/{incident_id}")).json()
    assert detail["status"] == "OPEN" and detail["assignee"] is None


async def test_other_organisation_cannot_read_people_devices_or_audit(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant
) -> None:
    intruder = await client_factory(other_tenant.emails[Role.ORGANISATION_ADMIN])

    assert (await intruder.get(f"/v1/service-users/{tenant.service_user_id}")).status_code == 404
    assert (
        await intruder.post(
            f"/v1/service-users/{tenant.service_user_id}/contacts",
            json={
                "full_name": "X",
                "relationship": "Y",
                "phone_number": "+447700900999",
                "priority": 3,
            },
        )
    ).status_code == 404
    own_users = (await intruder.get("/v1/service-users")).json()
    assert {u["id"] for u in own_users} == {str(other_tenant.service_user_id)}

    devices = (await intruder.get("/v1/devices")).json()
    assert tenant.device_external_id not in {d["external_id"] for d in devices}
    assert (
        await intruder.get("/v1/devices", params={"service_user_id": str(tenant.service_user_id)})
    ).json() == []

    users = (await intruder.get("/v1/users")).json()
    assert all(tenant.slug not in u["email"] for u in users)

    logs = (await intruder.get("/v1/audit-logs")).json()
    assert all(log["actor_name"] is None or tenant.slug not in log["actor_name"] for log in logs)


async def test_cannot_register_device_for_other_organisations_service_user(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant
) -> None:
    intruder = await client_factory(other_tenant.emails[Role.ORGANISATION_ADMIN])
    response = await intruder.post(
        "/v1/devices",
        json={
            "external_id": "ROGUE-1",
            "device_type": "SOS_PENDANT",
            "manufacturer": "Acme",
            "service_user_id": str(tenant.service_user_id),
        },
    )
    assert response.status_code == 404


async def test_simulator_cannot_target_another_organisations_device(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant, container: Container
) -> None:
    intruder = await client_factory(other_tenant.emails[Role.OPERATOR])
    response = await intruder.post(
        f"/v1/simulator/devices/{tenant.device_id}/events", json={"event_type": "SOS_BUTTON"}
    )
    assert response.status_code == 404
    assert await incident_count(container.session_factory, tenant.organisation_id) == 0


async def test_dashboard_counts_are_tenant_scoped(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant
) -> None:
    gateway = await client_factory()
    await raise_sos(gateway, tenant)
    mine = await client_factory(tenant.emails[Role.OPERATOR])
    theirs = await client_factory(other_tenant.emails[Role.OPERATOR])
    assert (await mine.get("/v1/dashboard/summary")).json()["critical"] == 1
    assert (await theirs.get("/v1/dashboard/summary")).json()["critical"] == 0


async def test_escalation_policies_are_tenant_scoped(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant
) -> None:
    owner = await client_factory(tenant.emails[Role.CARE_MANAGER])
    intruder = await client_factory(other_tenant.emails[Role.CARE_MANAGER])
    policy_id = (await owner.get("/v1/escalation-policies")).json()[0]["id"]
    response = await intruder.put(
        f"/v1/escalation-policies/{policy_id}",
        json={
            "name": "hijack",
            "steps": [{"delay_seconds": 0, "action_type": "OPERATOR_ESCALATION"}],
        },
    )
    assert response.status_code == 404


async def test_platform_admin_cannot_browse_tenant_care_data(
    client_factory: ClientFactory, tenant: Tenant, container: Container
) -> None:
    from careos.core.security import hash_password
    from careos.modules.identity.models import User

    async with container.session_factory() as session:
        session.add(
            User(
                organisation_id=None,
                email="platform@careos.example.com",
                full_name="Platform",
                role=Role.PLATFORM_ADMIN,
                password_hash=hash_password("Correct-Horse-Battery-9"),
            )
        )
        await session.commit()
    platform = await client_factory("platform@careos.example.com")
    assert (await platform.get("/v1/incidents")).status_code == 403
    assert (await platform.get(f"/v1/service-users/{tenant.service_user_id}")).status_code == 403
    assert (await platform.get("/v1/platform/organisations")).status_code == 200


async def test_realtime_hub_only_delivers_to_the_messages_organisation(
    tenant: Tenant, other_tenant: Tenant
) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_text(self, data: str) -> None:
            self.sent.append(data)

    hub = ConnectionHub()
    socket_a, socket_b = FakeSocket(), FakeSocket()
    await hub.register(tenant.organisation_id, socket_a)  # type: ignore[arg-type]
    await hub.register(other_tenant.organisation_id, socket_b)  # type: ignore[arg-type]
    await hub.deliver(
        RealtimeMessage(
            type=RealtimeMessageType.INCIDENT_CREATED, organisation_id=tenant.organisation_id
        )
    )
    assert len(socket_a.sent) == 1
    assert socket_b.sent == []


async def test_suspending_an_organisation_revokes_existing_sessions(
    client_factory: ClientFactory, tenant: Tenant, other_tenant: Tenant, container: Container
) -> None:
    from sqlalchemy import update

    from careos.modules.organisations.models import Organisation, OrganisationStatus

    operator = await client_factory(tenant.emails[Role.OPERATOR])
    unaffected = await client_factory(other_tenant.emails[Role.OPERATOR])
    assert (await operator.get("/v1/incidents")).status_code == 200

    async with container.session_factory() as session:
        await session.execute(
            update(Organisation)
            .where(Organisation.id == tenant.organisation_id)
            .values(status=OrganisationStatus.SUSPENDED)
        )
        await session.commit()

    assert (await operator.get("/v1/incidents")).status_code == 401
    assert (await unaffected.get("/v1/incidents")).status_code == 200
