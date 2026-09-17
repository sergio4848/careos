from __future__ import annotations

import pytest

from careos.core.config import Settings
from careos.core.logging import REDACTED, redact_sensitive
from careos.core.security import (
    csrf_token_for_session,
    gateway_key_digest,
    generate_gateway_key,
    hash_password,
    parse_gateway_key,
    session_token_digest,
    verify_password,
)
from careos.modules.identity.rbac import ROLE_PERMISSIONS, Permission, Role, permissions_for

SECRET = "unit-test-secret-0123456789-0123456789"


def test_password_hashing_uses_argon2id_and_verifies() -> None:
    hashed = hash_password("s3cure-Passphrase!")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "s3cure-Passphrase!")
    assert not verify_password(hashed, "wrong")
    assert not verify_password(None, "anything")  # unknown user still runs a verification
    assert not verify_password("not-a-hash", "anything")


def test_session_digest_and_csrf_are_keyed_and_bound() -> None:
    digest_a = session_token_digest(SECRET, "token-a")
    digest_b = session_token_digest(SECRET, "token-b")
    assert digest_a != digest_b
    assert session_token_digest("another-secret-0123456789-0123456789", "token-a") != digest_a
    assert csrf_token_for_session(SECRET, digest_a) != csrf_token_for_session(SECRET, digest_b)
    # domain separation: a session digest is never a valid CSRF token
    assert csrf_token_for_session(SECRET, digest_a) != digest_a


def test_gateway_keys_round_trip() -> None:
    key = generate_gateway_key()
    parsed = parse_gateway_key(key.raw)
    assert parsed is not None and parsed.prefix == key.prefix
    assert gateway_key_digest(SECRET, key.raw) == gateway_key_digest(SECRET, parsed.raw)
    for bad in ("", "cgk_short_x", "Bearer abc", "cgk_ABCDEFGH_" + "x" * 20):
        assert parse_gateway_key(bad) is None


def test_log_redaction_removes_secrets_and_pii() -> None:
    event = {
        "event": "login",
        "password": "hunter2",
        "session_token": "abc",
        "payload": {"phone_number": "+44 7700 900123", "battery": 80},
        "request_id": "r-1",
    }
    redacted = redact_sensitive(None, "info", event)
    assert redacted["password"] == REDACTED
    assert redacted["session_token"] == REDACTED
    assert redacted["payload"]["phone_number"] == REDACTED
    assert redacted["payload"]["battery"] == 80
    assert redacted["request_id"] == "r-1"


def test_production_rejects_insecure_configuration() -> None:
    with pytest.raises(ValueError, match="Unsafe production configuration"):
        Settings(
            environment="production",
            secret_key="dev-insecure-secret-key-do-not-use-in-production",
            cookie_secure=False,
            simulator_enabled=True,
            embedded_worker=True,
        )


def test_secret_key_minimum_length() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        Settings(secret_key="too-short")


def test_production_accepts_hardened_configuration() -> None:
    settings = Settings(
        environment="production",
        secret_key="Zq3v9mX2rL8tN5wK1pB7cF4hJ6dS0gY_prod",
        cookie_secure=True,
        simulator_enabled=False,
        cors_allowed_origins="https://console.careos.example",
    )
    assert settings.cors_allowed_origins == ["https://console.careos.example"]


def test_platform_admin_has_no_tenant_care_data_access() -> None:
    platform = permissions_for(Role.PLATFORM_ADMIN)
    assert Permission.INCIDENTS_READ not in platform
    assert Permission.SERVICE_USERS_READ not in platform
    assert platform == {Permission.PLATFORM_ORGANISATIONS_MANAGE}


def test_role_hierarchy_and_least_privilege() -> None:
    operator = permissions_for(Role.OPERATOR)
    manager = permissions_for(Role.CARE_MANAGER)
    admin = permissions_for(Role.ORGANISATION_ADMIN)
    assert operator < manager < admin
    assert Permission.USERS_MANAGE not in manager
    assert Permission.AUDIT_READ not in operator
    assert permissions_for(Role.TRUSTED_CONTACT) == frozenset()
    assert Permission.INCIDENTS_TAKEOVER not in permissions_for(Role.CAREGIVER)
    assert set(ROLE_PERMISSIONS) == set(Role)
