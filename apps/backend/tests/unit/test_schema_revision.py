"""/ready's schema check must track the real Alembic migration chain."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from careos.db.schema import (
    EXPECTED_SCHEMA_REVISION,
    SCHEMA_REVISIONS,
    classify_revision,
    is_safe,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _script_directory() -> ScriptDirectory:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return ScriptDirectory.from_config(config)


def test_expected_revision_is_the_single_alembic_head() -> None:
    script = _script_directory()
    assert script.get_heads() == [EXPECTED_SCHEMA_REVISION]


def test_known_revisions_match_the_migration_chain() -> None:
    script = _script_directory()
    chain = [rev.revision for rev in script.walk_revisions()]  # newest first
    assert tuple(reversed(chain)) == SCHEMA_REVISIONS


def test_revision_classification() -> None:
    assert classify_revision(EXPECTED_SCHEMA_REVISION) == "current"
    assert classify_revision("0001") == "outdated"
    assert classify_revision("9999_future") == "ahead"
    assert classify_revision(None) == "unknown"
    assert is_safe("current") and is_safe("ahead")
    assert not is_safe("outdated") and not is_safe("unknown")
