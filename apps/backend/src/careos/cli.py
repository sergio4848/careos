"""Operational CLI: ``python -m careos.cli <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from careos.contracts.device_events import CareOSEvent
from careos.contracts.realtime import RealtimeMessage
from careos.core.config import Settings, get_settings
from careos.core.logging import configure_logging
from careos.core.security import gateway_key_digest, generate_gateway_key
from careos.db.session import create_engine, create_session_factory
from careos.modules.device_gateway.models import GatewayCredential
from careos.modules.organisations.models import Organisation
from careos.seed.demo import seed_demo

REPO_ROOT = Path(__file__).resolve().parents[4]


async def _seed(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        report = await seed_demo(create_session_factory(engine), settings)
    finally:
        await engine.dispose()
    print("Demo seed:", "created" if report.created else "skipped (already present)")
    for note in report.notes:
        print(" -", note)
    for email, role in report.logins:
        print(f"   login {email:<36} role {role}  (password: CAREOS_SEED_DEMO_PASSWORD)")


async def _create_gateway_key(settings: Settings, slug: str, name: str) -> None:
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            organisation = await session.scalar(
                select(Organisation).where(Organisation.slug == slug)
            )
            if organisation is None:
                raise SystemExit(f"organisation '{slug}' not found")
            key = generate_gateway_key()
            session.add(
                GatewayCredential(
                    organisation_id=organisation.id,
                    name=name,
                    key_prefix=key.prefix,
                    key_digest=gateway_key_digest(settings.secret_key.get_secret_value(), key.raw),
                )
            )
            await session.commit()
    finally:
        await engine.dispose()
    print("Gateway key (shown once, store it in your secrets manager):")
    print(key.raw)


def export_contracts(out_dir: Path) -> list[Path]:
    """Write JSON Schemas generated from the Pydantic contracts (single source of truth)."""
    schemas = {
        "careos-device-event.v1.schema.json": CareOSEvent.model_json_schema(),
        "careos-realtime-message.v1.schema.json": RealtimeMessage.model_json_schema(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, schema in schemas.items():
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
        path = out_dir / filename
        content = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        path.write_text(content, encoding="utf-8", newline="\n")  # identical output on every OS
        written.append(path)
    return written


def export_openapi(out_file: Path) -> Path:
    from careos.api.app import create_app

    settings = get_settings()
    app = create_app(settings.model_copy(update={"simulator_enabled": True}))
    out_file.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    out_file.write_text(content, encoding="utf-8", newline="\n")
    return out_file


def main() -> None:
    parser = argparse.ArgumentParser(prog="careos")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed-demo", help="Seed Demo Care UK (development only)")
    key = sub.add_parser("create-gateway-key", help="Issue a device gateway credential")
    key.add_argument("--organisation-slug", required=True)
    key.add_argument("--name", required=True)
    exp = sub.add_parser(
        "export-contracts", help="Write JSON Schemas and OpenAPI to packages/contracts"
    )
    exp.add_argument("--out", type=Path, default=REPO_ROOT / "packages" / "contracts")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    if args.command == "seed-demo":
        asyncio.run(_seed(settings))
    elif args.command == "create-gateway-key":
        asyncio.run(_create_gateway_key(settings, args.organisation_slug, args.name))
    elif args.command == "export-contracts":
        for path in export_contracts(args.out / "schemas"):
            print("wrote", path)
        print("wrote", export_openapi(args.out / "openapi" / "careos-api.v1.json"))


if __name__ == "__main__":
    main()
