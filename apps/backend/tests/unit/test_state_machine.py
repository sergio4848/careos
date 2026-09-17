from __future__ import annotations

import pytest

from careos.core.errors import InvalidStateTransitionError
from careos.modules.incident_engine.state_machine import (
    ACTIVE_STATUSES,
    AWAITING_CLOSURE_STATUSES,
    TRANSITIONS,
    IncidentStatus,
    assert_transition,
    can_transition,
)

S = IncidentStatus


def test_every_status_declares_transitions() -> None:
    assert set(TRANSITIONS) == set(IncidentStatus)


def test_closed_is_terminal() -> None:
    for target in IncidentStatus:
        assert not can_transition(S.CLOSED, target)


def test_closed_cannot_reopen() -> None:
    with pytest.raises(InvalidStateTransitionError) as exc:
        assert_transition(S.CLOSED, S.OPEN)
    assert exc.value.details == {"from": "CLOSED", "to": "OPEN"}


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.RECEIVED, S.VALIDATING),
        (S.VALIDATING, S.OPEN),
        (S.OPEN, S.CONTACTING),
        (S.CONTACTING, S.ACKNOWLEDGED),
        (S.ACKNOWLEDGED, S.IN_PROGRESS),
        (S.IN_PROGRESS, S.RESOLVED),
        (S.RESOLVED, S.CLOSED),
    ],
)
def test_happy_path_is_allowed(current: IncidentStatus, target: IncidentStatus) -> None:
    assert_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.RECEIVED, S.OPEN),  # validation cannot be skipped
        (S.OPEN, S.CLOSED),  # must be resolved first
        (S.IN_PROGRESS, S.CLOSED),
        (S.RESOLVED, S.OPEN),
        (S.FALSE_ALARM, S.ESCALATED),
        (S.CANCELLED, S.OPEN),
        (S.FAILED, S.CLOSED),  # automation failure can never silently end an incident
    ],
)
def test_invalid_transitions_are_rejected(current: IncidentStatus, target: IncidentStatus) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidStateTransitionError):
        assert_transition(current, target)


def test_self_transitions_are_never_allowed() -> None:
    for status in IncidentStatus:
        assert not can_transition(status, status)


def test_status_groups_are_disjoint_and_consistent() -> None:
    assert ACTIVE_STATUSES.isdisjoint(AWAITING_CLOSURE_STATUSES)
    assert S.CLOSED not in ACTIVE_STATUSES | AWAITING_CLOSURE_STATUSES
    for status in AWAITING_CLOSURE_STATUSES:
        assert can_transition(status, S.CLOSED)
    for status in ACTIVE_STATUSES - {S.RECEIVED, S.VALIDATING}:
        # every active incident a human can see must be resolvable
        assert can_transition(status, S.RESOLVED) or can_transition(status, S.FALSE_ALARM)
