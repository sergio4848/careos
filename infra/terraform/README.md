# Infrastructure as code (placeholder)

Intentionally empty in the MVP: the sprint goal is a reproducible local stack
(`docker compose up`). No cloud resources are provisioned from this repository yet.

Target shape for the first hosted environment (UK region):

| Concern | Proposal |
|---|---|
| Compute | container service (AWS ECS Fargate or Azure Container Apps): `web`, `api` (≥2), `worker` (≥2) |
| Database | managed PostgreSQL 18, multi-AZ, PITR backups, encryption at rest |
| Redis | managed Redis 8 (ElastiCache / Azure Cache), TLS |
| Edge | TLS termination + WAF; `/v1/gateway/*` on a separate hostname with stricter limits |
| Secrets | cloud secrets manager injected as environment variables |
| Observability | log shipping (JSON), Prometheus-compatible metrics scraping, alerting on `/ready`, worker heartbeat and escalation lag |
| State | Terraform remote state with locking, one workspace per environment |

Modules will live in `infra/terraform/modules/*` with environments in `infra/terraform/envs/*`.
