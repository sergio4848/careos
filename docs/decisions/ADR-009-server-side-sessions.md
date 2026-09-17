# ADR-009: Server-side sessions instead of JWT for the console

* Status: Accepted · 2026-09-16

## Context

Operators work long shifts on shared control-room PCs. We need immediate revocation (a leaver,
a lost laptop, a role change), no tokens readable by JavaScript, and CSRF protection.

## Decision

* Login issues a 256-bit random token in an **HttpOnly, SameSite=Lax** cookie
  (`Secure` + optional parent `Domain` in production).
* Only an **HMAC-SHA256 digest** of the token is stored (`user_sessions.token_digest`), so a
  database read does not yield usable sessions.
* Absolute TTL (default 12 h) and idle timeout (default 2 h); `last_seen_at` is touched at most
  once a minute.
* **CSRF:** synchroniser token = HMAC of the session digest, returned by `/v1/auth/login` and
  `/v1/auth/me` (readable only by allowed origins through CORS), held in memory by the console
  and required as `X-CSRF-Token` on every state-changing request.
* Revocation: logout, role change (all sessions of that user), and the WebSocket re-validates.
* Passwords: Argon2id; login always performs a hash verification (no user enumeration by timing);
  rate limited per account and per IP; failures audited.
* Machine clients (device platforms) use separate **gateway credentials** (prefix + HMAC digest).

## Consequences

* One indexed lookup per request (cacheable later). No JWT algorithm/key-rotation pitfalls.
* Not suitable for third-party API consumers; when needed, add OAuth2 client credentials
  with scoped, short-lived tokens rather than reusing console sessions.
* SSO (Azure AD / Google Workspace via OIDC) can create the same server-side session.
