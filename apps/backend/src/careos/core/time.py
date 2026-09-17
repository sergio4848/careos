from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Timezone-aware UTC now. The only clock the domain uses (easy to patch in tests)."""
    return datetime.now(UTC)
