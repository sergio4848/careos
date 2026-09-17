# Security

CareOS processes personal data of vulnerable people and drives safety-relevant workflows.
This document lists the controls implemented in the MVP, how they are verified, and the known
gaps before production.

## Implemented controls

| Area | Control | Where / verified by |
|---|---|---|
| Passwords | Argon2id, timing-equalised verification, rehash on parameter change | `core/security.py`, `test_security_and_config.py` |
| Sessions | 256-bit opaque token in HttpOnly SameSite=Lax cookie; only HMAC digest stored; absolute + idle expiry; revocation on logout and role change | ADR-009, `test_auth.py` |
| CSRF | session-bound HMAC token required on unsafe methods | `api/deps.py`, `test_state_changes_require_csrf_token` |
| Brute force | login rate limit per account and per IP (Redis, in-memory fallback); gateway rate limit per credential | `core/rate_limit.py`, `test_login_is_rate_limited` |
| Authorisation | central permission dependency; denials audited | `identity/rbac.py`, `api/deps.py` |
| Tenant isolation | principal-derived scope, 404 on cross-tenant, composite FKs, partitioned realtime | ADR-005, `test_tenant_isolation.py` |
| Device ingestion | tenant-bound gateway keys (prefix + HMAC digest), strict schema (`extra=forbid`), size limits, clock-skew check | `device_gateway/`, `test_gateway.py` |
| Input validation | Pydantic on every request; 256 KB body limit; validation errors never echo values | `api/middleware.py`, `api/exception_handlers.py` |
| SQL injection | SQLAlchemy parameterised queries only; no string-built SQL with user input | code review, ruff `S` rules |
| XSS | React escaping; no `dangerouslySetInnerHTML` (ESLint rule); CSP on web (runtime `connect-src`) and `default-src 'none'` on API | `apps/web/src/proxy.ts`, `eslint.config.mjs` |
| Security headers | nosniff, frame DENY, referrer policy, COOP, permissions policy, HSTS when HTTPS, `Cache-Control: no-store` on API | `SecurityHeadersMiddleware` |
| WebSocket | cookie auth, Origin allow-list (CSWSH), periodic session re-validation | `realtime/router.py`, `test_socket_requires_session_and_allowed_origin` |
| Secrets | environment only; `.env` git-ignored; production refuses placeholder secrets, insecure cookies, enabled simulator, wildcard CORS | `core/config.py`, `test_production_rejects_insecure_configuration` |
| Logging | structured JSON; redaction of passwords, tokens, cookies, phone, email, notes, address, location keys; no query strings in access logs | `core/logging.py` |
| Audit | immutable (`BEFORE UPDATE OR DELETE` trigger), written in the action's transaction; failures written in an isolated transaction | ADR-010, `test_timeline_and_audit_records_are_immutable` |
| Containers | non-root users, slim/alpine bases, ports bound to 127.0.0.1 in compose | Dockerfiles, `docker-compose.yml` |
| Supply chain | exact pins, lockfile, gitleaks + detect-private-key pre-commit | ADR-011, `.pre-commit-config.yaml` |

## Audited actions

`AUTH_LOGIN_SUCCEEDED`, `AUTH_LOGIN_FAILED`, `AUTH_LOGOUT`, `ACCESS_DENIED`, `ORGANISATION_CREATED`,
`USER_CREATED`, `USER_PERMISSIONS_CHANGED`, `SERVICE_USER_CREATED`, `SERVICE_USER_UPDATED`,
`SERVICE_USER_VIEWED`, `TRUSTED_CONTACT_CREATED`, `TRUSTED_CONTACT_UPDATED`, `DEVICE_ADDED`,
`ESCALATION_POLICY_CREATED`, `ESCALATION_POLICY_CHANGED`,
`GATEWAY_AUTH_FAILED`, `GATEWAY_EVENT_REJECTED`, `SIMULATOR_EVENT_SENT`, `INCIDENT_CREATED`,
`INCIDENT_VIEWED`, `INCIDENT_TAKEOVER`, `INCIDENT_STATE_CHANGED`, `INCIDENT_RESOLVED`,
`INCIDENT_CLOSED`.

## Threat model (summary, STRIDE)

| Threat | Example | Mitigation |
|---|---|---|
| Spoofing | forged SOS for another tenant | tenant-bound gateway keys, device resolved inside tenant |
| Tampering | editing the timeline after an adverse event | append-only trigger, no update API, audit trail |
| Repudiation | "I never resolved that" | user-attributed events + audit with request ID and IP |
| Information disclosure | operator of provider B browses provider A | 404 isolation, composite FKs, tests |
| Denial of service | event flood, huge payloads | per-credential rate limits, body limit, `SKIP LOCKED` worker scaling |
| Elevation of privilege | admin grants PLATFORM_ADMIN | assignable-role allow-list, self-role-change blocked |

## Known gaps before production (tracked)

1. PostgreSQL row-level security as defence in depth (ADR-005).
2. MFA / SSO (OIDC) for staff; password policy and breach-password check.
3. Nonce-based CSP (remove `'unsafe-inline'` for scripts).
4. Secrets manager integration (AWS Secrets Manager / Azure Key Vault) and key rotation for
   `CAREOS_SECRET_KEY` (dual-key verification window).
5. Encryption of selected columns (phone numbers, resolution notes) with envelope encryption.
6. Python dependency lockfile + SCA (pip-audit/Dependabot) and container image scanning (Trivy).
7. Penetration test and DSPT (NHS Data Security and Protection Toolkit) assessment.
8. Restrict `/metrics` and `/docs` at the ingress; WAF in front of the gateway.
