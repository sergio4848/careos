"""Twilio → CareOS state mapping. Twilio's vocabulary never leaks past this module."""

from __future__ import annotations

from careos.modules.notification_engine.models import (
    CALL_STATUS_RANK,
    TERMINAL_CALL_STATUSES,
    CallStatus,
)
from careos.modules.telephony.models import CallEventType

#: Twilio status callback values → CareOS internal call lifecycle.
TWILIO_STATUS_MAP: dict[str, CallStatus] = {
    "queued": CallStatus.QUEUED,
    "initiated": CallStatus.INITIATED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.BUSY,
    "no-answer": CallStatus.NO_ANSWER,
    "failed": CallStatus.FAILED,
    "canceled": CallStatus.CANCELLED,
}

STATUS_EVENT_MAP: dict[CallStatus, CallEventType] = {
    CallStatus.QUEUED: CallEventType.CALL_REQUESTED,
    CallStatus.INITIATED: CallEventType.CALL_INITIATED,
    CallStatus.RINGING: CallEventType.CALL_RINGING,
    CallStatus.IN_PROGRESS: CallEventType.CALL_ANSWERED,
    CallStatus.COMPLETED: CallEventType.CALL_ENDED,
    CallStatus.BUSY: CallEventType.CALL_BUSY,
    CallStatus.NO_ANSWER: CallEventType.CALL_NO_ANSWER,
    CallStatus.FAILED: CallEventType.CALL_FAILED,
    CallStatus.CANCELLED: CallEventType.CALL_CANCELLED,
    CallStatus.TIMED_OUT: CallEventType.CALL_TIMED_OUT,
}


def map_twilio_status(value: str) -> CallStatus | None:
    return TWILIO_STATUS_MAP.get(value.strip().lower())


def may_advance(current: CallStatus, target: CallStatus) -> bool:
    """Forward-only: never leave a terminal state, never move backwards (replay safety)."""
    if current in TERMINAL_CALL_STATUSES:
        return False
    return CALL_STATUS_RANK[target] > CALL_STATUS_RANK[current]
