# ADR-011: Dependency and version policy

* Status: Accepted · 2026-09-16

## Decision

* Use the latest **stable** release of each direct dependency at the time of writing; no
  pre-release, beta or experimental packages.
* Pin direct dependencies exactly (`==` in `pyproject.toml`, exact versions in `package.json`);
  commit `package-lock.json`. Update deliberately through pull requests (Dependabot/Renovate).
* Runtime baselines: Python 3.13, Node.js 24 LTS, PostgreSQL 18, Redis 8.

Versions at bootstrap (2026-09-16):

| Area | Packages |
|---|---|
| Backend | FastAPI 0.141.1, Uvicorn 0.53.0, Pydantic 2.13.5, pydantic-settings 2.15.0, SQLAlchemy 2.0.54, asyncpg 0.31.0, Alembic 1.20.0, redis-py 8.1.0, argon2-cffi 25.1.0, structlog 26.1.0, prometheus-client 0.26.0, httpx 0.28.1 |
| Backend dev | pytest 9.1.1, pytest-asyncio 1.4.0, ruff 0.16.8, mypy 2.3.1 |
| Web | Next.js 16.3.5, React 19.3.0, TanStack Query 5.103.1, TypeScript 6.0.3 |
| Web dev | Vitest 5.0.1, Vite 8.3.0, Testing Library, openapi-typescript 7.13.0 |

### Known, deliberate exceptions

* **ESLint 9.39.5 instead of 10.x.** `eslint-config-next@16.3.5` bundles `eslint-plugin-react`,
  which calls APIs removed in ESLint 10 (`context.getFilename`), so linting crashes on 10.x.
  Upgrade when the Next.js ESLint config supports ESLint 10.
* **TypeScript 6.0.3 instead of 7.x.** `typescript-eslint` supports `<6.1`. `openapi-typescript`
  declares a `^5` peer; an npm `overrides` entry pins it to the workspace TypeScript, which
  type-generation works with.
* **Python lockfile.** Transitive Python dependencies are not locked yet. Next step: adopt
  `uv lock` (or pip-tools) and install from the lock in Docker and CI.
