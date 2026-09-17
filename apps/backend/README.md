# CareOS backend

Python modular monolith (FastAPI + SQLAlchemy 2 + PostgreSQL) with two entrypoints:

| Entrypoint | Command | Responsibility |
|---|---|---|
| API | `uvicorn careos.api.app:create_app --factory` | REST, WebSocket, device gateway |
| Worker | `python -m careos.worker` | Durable escalation timers, device health sweep |

See the repository [README](../../README.md) and [architecture docs](../../docs/architecture/overview.md).

## Layout

```
src/careos/
  core/        config, logging, errors, security primitives, rate limiting, metrics
  db/          declarative base, session factory, unit of work, model registry
  contracts/   CareOS Device Event Contract + realtime message contract (source of truth)
  modules/     bounded contexts (incident_engine, escalation_engine, device_gateway, ...)
  api/         FastAPI app factory, middleware, dependencies, health
  worker/      worker process
  cli.py       seed-demo, create-gateway-key, export-contracts
migrations/    Alembic revisions
tests/         pytest (unit + PostgreSQL integration)
```

## Common commands

```bash
pip install -e ".[dev]"
alembic upgrade head
python -m careos.cli seed-demo
pytest
ruff check . && ruff format --check . && mypy
```
