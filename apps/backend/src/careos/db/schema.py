"""Database schema revisions this build understands (checked by ``/ready``).

``tests/unit/test_schema_revision.py`` fails if this list and the Alembic migration chain
ever differ, so a new migration cannot ship without updating readiness.
"""

from __future__ import annotations

from typing import Literal

#: Alembic revisions in order, oldest first.
SCHEMA_REVISIONS: tuple[str, ...] = ("0001", "0002")
EXPECTED_SCHEMA_REVISION = SCHEMA_REVISIONS[-1]

SchemaState = Literal["current", "ahead", "outdated", "unknown"]


def classify_revision(revision: str | None) -> SchemaState:
    """``outdated``: the database lacks tables/constraints this code relies on (not safe).
    ``ahead``: a newer release migrated first; expand/contract migrations keep it compatible."""
    if revision is None:
        return "unknown"
    if revision == EXPECTED_SCHEMA_REVISION:
        return "current"
    if revision in SCHEMA_REVISIONS:
        return "outdated"
    return "ahead"


def is_safe(state: SchemaState) -> bool:
    return state in ("current", "ahead")
