"""Twilio request authentication and telephone-number hygiene.

Twilio signs every webhook with ``X-Twilio-Signature``:
``base64(HMAC-SHA1(auth_token, url + concat(sorted(k + v))))`` over the exact URL Twilio
was given and the POST form fields. We reconstruct the URL from the *configured* public
base (never from the Host header, which a client controls).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

E164_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")


def is_e164(number: str) -> bool:
    return bool(E164_PATTERN.match(number))


def mask_number(number: str) -> str:
    """``+447700900123`` → ``+44*******123``. Full numbers are never persisted or logged."""
    if len(number) <= 6:
        return "*" * len(number)
    prefix, last = number[:3], number[-3:]
    return f"{prefix}{'*' * (len(number) - 6)}{last}"


def compute_twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    payload = url + "".join(key + params[key] for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_twilio_signature(
    auth_token: str, url: str, params: dict[str, str], signature: str | None
) -> bool:
    if not signature:
        return False
    expected = compute_twilio_signature(auth_token, url, params)
    return hmac.compare_digest(expected, signature)


def new_media_token() -> str:
    """Opaque single-use token binding one Twilio media stream to one CareOS call."""
    return secrets.token_urlsafe(32)


def media_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
