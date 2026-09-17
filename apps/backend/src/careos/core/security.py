"""Security primitives: password hashing, opaque tokens, CSRF and gateway keys.

* Passwords: Argon2id (argon2-cffi, RFC 9106 parameters).
* Session tokens: 256-bit random, only an HMAC-SHA256 digest is stored.
* CSRF: HMAC bound to the session, sent as a readable cookie + request header.
* Gateway keys: ``cgk_<prefix>_<secret>``; lookup by prefix, HMAC digest compared
  in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_password_hasher = PasswordHasher()

# Pre-computed hash used to equalise timing when the user does not exist.
_DUMMY_PASSWORD_HASH = _password_hasher.hash("careos-timing-equaliser")


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Verify a password. Always performs an Argon2 verification to avoid user enumeration."""
    try:
        return _password_hasher.verify(password_hash or _DUMMY_PASSWORD_HASH, password) and (
            password_hash is not None
        )
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _password_hasher.check_needs_rehash(password_hash)


def generate_token(num_bytes: int = 32) -> str:
    return secrets.token_urlsafe(num_bytes)


def hmac_digest(secret: str, value: str, *, purpose: str) -> str:
    """Keyed digest with domain separation (``purpose``) so digests are never interchangeable."""
    return hmac.new(secret.encode(), f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def session_token_digest(secret: str, token: str) -> str:
    return hmac_digest(secret, token, purpose="session")


def csrf_token_for_session(secret: str, session_digest: str) -> str:
    return hmac_digest(secret, session_digest, purpose="csrf")


# --------------------------------------------------------------------------- gateway keys

_GATEWAY_KEY_RE = re.compile(r"^cgk_(?P<prefix>[a-z0-9]{8})_(?P<secret>[A-Za-z0-9_\-]{16,128})$")


@dataclass(frozen=True, slots=True)
class ParsedGatewayKey:
    prefix: str
    raw: str


def parse_gateway_key(raw_key: str) -> ParsedGatewayKey | None:
    match = _GATEWAY_KEY_RE.match(raw_key.strip())
    if match is None:
        return None
    return ParsedGatewayKey(prefix=match.group("prefix"), raw=raw_key.strip())


def generate_gateway_key() -> ParsedGatewayKey:
    prefix = secrets.token_hex(4)
    raw = f"cgk_{prefix}_{secrets.token_urlsafe(32)}"
    return ParsedGatewayKey(prefix=prefix, raw=raw)


def gateway_key_digest(secret: str, raw_key: str) -> str:
    return hmac_digest(secret, raw_key, purpose="gateway-key")
