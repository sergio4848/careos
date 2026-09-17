"""Incident lifecycle state machine — the single authority on legal status changes.

Pure, deterministic and dependency-free so it can be exhaustively unit tested.
No controller, worker or provider may assign ``incident.status`` directly; every change
goes through :func:`assert_transition` (via ``IncidentEngine._transition``).

Lifecycle (happy path)::

    RECEIVED -> VALIDATING -> OPEN -> CONTACTING -> ACKNOWLEDGED
             -> IN_PROGRESS -> RESOLVED -> CLOSED

Exceptional states: ESCALATED, FALSE_ALARM, CANCELLED, FAILED, DEVICE_ERROR.
CLOSED is terminal. RESOLVED is *not* closed: an incident can be re-opened to
IN_PROGRESS until a human closes it (docs/architecture/incident-lifecycle.md).
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType

from careos.core.errors import InvalidStateTransitionError


class IncidentStatus(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    OPEN = "OPEN"
    CONTACTING = "CONTACTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    # exceptional
    ESCALATED = "ESCALATED"
    FALSE_ALARM = "FALSE_ALARM"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    DEVICE_ERROR = "DEVICE_ERROR"


S = IncidentStatus

_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    S.RECEIVED: frozenset({S.VALIDATING, S.FAILED, S.DEVICE_ERROR}),
    S.VALIDATING: frozenset({S.OPEN, S.DEVICE_ERROR, S.FAILED, S.CANCELLED}),
    S.OPEN: frozenset(
        {
            S.CONTACTING,
            S.ACKNOWLEDGED,
            S.IN_PROGRESS,
            S.ESCALATED,
            S.RESOLVED,
            S.FALSE_ALARM,
            S.CANCELLED,
            S.FAILED,
        }
    ),
    S.CONTACTING: frozenset(
        {
            S.ACKNOWLEDGED,
            S.ESCALATED,
            S.IN_PROGRESS,
            S.RESOLVED,
            S.FALSE_ALARM,
            S.CANCELLED,
            S.FAILED,
        }
    ),
    S.ESCALATED: frozenset(
        {S.CONTACTING, S.ACKNOWLEDGED, S.IN_PROGRESS, S.RESOLVED, S.FALSE_ALARM, S.CANCELLED}
    ),
    S.ACKNOWLEDGED: frozenset({S.IN_PROGRESS, S.ESCALATED, S.RESOLVED, S.FALSE_ALARM}),
    S.IN_PROGRESS: frozenset({S.ESCALATED, S.RESOLVED, S.FALSE_ALARM}),
    # Automation failed: a human must pick it up. It can never silently end.
    S.FAILED: frozenset({S.IN_PROGRESS, S.ESCALATED, S.RESOLVED, S.FALSE_ALARM}),
    S.DEVICE_ERROR: frozenset({S.IN_PROGRESS, S.ESCALATED, S.RESOLVED, S.FALSE_ALARM}),
    S.RESOLVED: frozenset({S.CLOSED, S.IN_PROGRESS}),
    S.FALSE_ALARM: frozenset({S.CLOSED, S.IN_PROGRESS}),
    S.CANCELLED: frozenset({S.CLOSED}),
    S.CLOSED: frozenset(),
}

TRANSITIONS: MappingProxyType[IncidentStatus, frozenset[IncidentStatus]] = MappingProxyType(
    _TRANSITIONS
)

#: Incident still needs attention (shown on the live board, blocks a second incident per device).
ACTIVE_STATUSES: frozenset[IncidentStatus] = frozenset(
    {
        S.RECEIVED,
        S.VALIDATING,
        S.OPEN,
        S.CONTACTING,
        S.ACKNOWLEDGED,
        S.IN_PROGRESS,
        S.ESCALATED,
        S.FAILED,
        S.DEVICE_ERROR,
    }
)
#: Outcome recorded, waiting for a human to close.
AWAITING_CLOSURE_STATUSES: frozenset[IncidentStatus] = frozenset(
    {S.RESOLVED, S.FALSE_ALARM, S.CANCELLED}
)
TERMINAL_STATUSES: frozenset[IncidentStatus] = frozenset({S.CLOSED})
#: Statuses from which an operator may take ownership.
TAKEOVER_STATUSES: frozenset[IncidentStatus] = frozenset(
    {S.OPEN, S.CONTACTING, S.ACKNOWLEDGED, S.ESCALATED, S.FAILED, S.DEVICE_ERROR, S.IN_PROGRESS}
)

if set(_TRANSITIONS) != set(IncidentStatus):  # pragma: no cover - guards future edits
    raise RuntimeError("every IncidentStatus must declare its allowed transitions")


def can_transition(current: IncidentStatus, target: IncidentStatus) -> bool:
    return target in _TRANSITIONS[current]


def assert_transition(current: IncidentStatus, target: IncidentStatus) -> None:
    if not can_transition(current, target):
        raise InvalidStateTransitionError(
            f"Incident cannot move from {current.value} to {target.value}.",
            details={"from": current.value, "to": target.value},
        )


def sql_status_list(statuses: frozenset[IncidentStatus]) -> str:
    """Render a status set for static SQL predicates (partial indexes); values are constants."""
    return ", ".join(f"'{status.value}'" for status in sorted(statuses))
