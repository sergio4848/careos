"""Call state persistence shared by the webhook handlers, the media bridge and the
Twilio provider's completion wait.

Every method runs its own short transaction and releases side effects only after commit
(UnitOfWork). Nothing here ever changes ``Incident.status``: status transitions remain
the escalation executor's job, so a webhook or media event can never resolve, close or
reopen an incident (ADR-016), and a callback arriving after manual resolution only
updates the call record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from careos.contracts.realtime import RealtimeMessage, RealtimeMessageType, RealtimePublisher
from careos.core.logging import get_logger
from careos.core.metrics import CALL_DURATION
from careos.core.time import utcnow
from careos.db.uow import UnitOfWork
from careos.modules.ai_orchestrator.models import AISession, AISessionPurpose, AISessionStatus
from careos.modules.ai_orchestrator.voice import VoiceResult
from careos.modules.escalation_engine import scheduler
from careos.modules.incident_engine.models import ActorType, Incident, IncidentEventType
from careos.modules.incident_engine.service import IncidentEngine
from careos.modules.notification_engine.models import (
    TERMINAL_CALL_STATUSES,
    Call,
    CallStatus,
    StructuredCallResponse,
)
from careos.modules.service_users.models import ServiceUser
from careos.modules.telephony.models import CallEvent, CallEventType
from careos.modules.telephony.security import media_token_digest
from careos.modules.telephony.states import STATUS_EVENT_MAP, map_twilio_status, may_advance

log = get_logger(__name__)

GATHER_RESPONSES: dict[str, StructuredCallResponse] = {
    "1": StructuredCallResponse.CAN_RESPOND,
    "2": StructuredCallResponse.CANNOT_RESPOND,
    "3": StructuredCallResponse.REQUEST_OPERATOR,
}


@dataclass(frozen=True, slots=True)
class CallSnapshot:
    status: CallStatus
    acknowledged: bool
    provider_call_sid: str | None
    structured_response: StructuredCallResponse | None
    answered: bool


@dataclass(frozen=True, slots=True)
class AppliedStatus:
    accepted: bool
    reason: str  # "applied", "duplicate", "stale", "unknown_call", "sid_mismatch", ...
    organisation_id: uuid.UUID | None = None
    incident_id: uuid.UUID | None = None
    status: CallStatus | None = None


@dataclass(frozen=True, slots=True)
class MediaContext:
    """Server-side binding of one media stream to one call. Built from the opaque token;
    a public client can never choose the organisation or incident it belongs to."""

    organisation_id: uuid.UUID
    incident_id: uuid.UUID
    call_id: uuid.UUID
    preferred_name: str
    language: str
    trigger_type: str


def event_row(
    *,
    organisation_id: uuid.UUID,
    incident_id: uuid.UUID,
    call_id: uuid.UUID,
    event_type: CallEventType,
    provider_event_key: str | None = None,
    data: dict[str, object] | None = None,
) -> dict[str, object]:
    now = utcnow()
    return {
        "id": uuid.uuid4(),
        "organisation_id": organisation_id,
        "incident_id": incident_id,
        "call_id": call_id,
        "event_type": event_type,
        "provider_event_key": provider_event_key,
        "data": data or {},
        "occurred_at": now,
        "created_at": now,
    }


class CallControlStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: RealtimePublisher,
        *,
        publish_timeout_seconds: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._publish_timeout = publish_timeout_seconds

    def _uow(self, session: AsyncSession) -> UnitOfWork:
        return UnitOfWork(session, self._publisher, publish_timeout_seconds=self._publish_timeout)

    @staticmethod
    def _notify(uow: UnitOfWork, call: Call, event_type: str) -> None:
        uow.notify(
            RealtimeMessage(
                type=RealtimeMessageType.INCIDENT_UPDATED,
                organisation_id=call.organisation_id,
                incident_id=call.incident_id,
                payload={"event_type": event_type},
            )
        )

    @staticmethod
    async def _locked_call(session: AsyncSession, call_id: uuid.UUID) -> Call | None:
        return await session.scalar(select(Call).where(Call.id == call_id).with_for_update())

    # ------------------------------------------------------------------ provider wait

    async def snapshot(self, call_id: uuid.UUID) -> CallSnapshot | None:
        async with self._session_factory() as session:
            call = await session.get(Call, call_id)
            if call is None:
                return None
            return CallSnapshot(
                status=call.status,
                acknowledged=call.acknowledged,
                provider_call_sid=call.provider_call_sid,
                structured_response=call.structured_response,
                answered=call.answered_at is not None,
            )

    async def bind_provider_sid(
        self, call_id: uuid.UUID, sid: str, *, from_number_masked: str | None
    ) -> None:
        """Persisted immediately after Twilio accepts, to shrink the crash window in which
        an accepted call is not yet linked to its row (ADR-017)."""
        async with self._session_factory() as session:
            call = await self._locked_call(session, call_id)
            if call is None:
                return
            call.provider_call_sid = call.provider_call_sid or sid
            call.from_number_masked = call.from_number_masked or from_number_masked
            if may_advance(call.status, CallStatus.INITIATED):
                call.status = CallStatus.INITIATED
            await session.commit()

    async def mark_timed_out(self, call_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            uow = self._uow(session)
            call = await self._locked_call(session, call_id)
            if call is None or call.status in TERMINAL_CALL_STATUSES:
                await session.commit()
                return
            call.status = CallStatus.TIMED_OUT
            call.ended_at = call.ended_at or utcnow()
            call.failure_category = "timeout"
            await session.execute(
                pg_insert(CallEvent).values(
                    event_row(
                        organisation_id=call.organisation_id,
                        incident_id=call.incident_id,
                        call_id=call.id,
                        event_type=CallEventType.CALL_TIMED_OUT,
                        data={"reason": "max_call_duration"},
                    )
                )
            )
            self._notify(uow, call, "CALL_STATUS")
            await uow.commit()

    # ------------------------------------------------------------------ status webhooks

    async def apply_provider_status(
        self,
        call_id: uuid.UUID,
        *,
        provider_sid: str,
        twilio_status: str,
        sequence: str | None,
        call_duration: str | None,
        error_code: str | None,
    ) -> AppliedStatus:
        status = map_twilio_status(twilio_status)
        if status is None:
            return AppliedStatus(False, "unknown_status")
        async with self._session_factory() as session:
            uow = self._uow(session)
            call = await self._locked_call(session, call_id)
            if call is None:
                return AppliedStatus(False, "unknown_call")
            if call.provider_call_sid is None:
                # First callback after a crash between the provider accept and the SID
                # persist: the callback URL's opaque call id re-links them (ADR-017).
                call.provider_call_sid = provider_sid
            elif call.provider_call_sid != provider_sid:
                return AppliedStatus(False, "sid_mismatch", organisation_id=call.organisation_id)
            key = f"status:{provider_sid}:{twilio_status}:{sequence or ''}"
            inserted = await self._insert_deduped(session, call, status, key, twilio_status)
            if inserted is None:
                await session.commit()
                return AppliedStatus(
                    True,
                    "duplicate",
                    organisation_id=call.organisation_id,
                    incident_id=call.incident_id,
                    status=call.status,
                )
            advanced = may_advance(call.status, status)
            if advanced:
                call.status = status
                if status is CallStatus.IN_PROGRESS:
                    call.answered_at = call.answered_at or utcnow()
                if status in TERMINAL_CALL_STATUSES:
                    call.ended_at = call.ended_at or utcnow()
                    if call_duration and call_duration.isdigit():
                        call.duration_seconds = int(call_duration)
                        provider, seconds = call.provider, int(call_duration)
                        uow.after_commit(lambda: CALL_DURATION.labels(provider).observe(seconds))
                    if status is CallStatus.FAILED:
                        call.failure_category = "provider_error"
                        call.failure_code = (error_code or "")[:64] or None
            self._notify(uow, call, "CALL_STATUS")
            await uow.commit()
            return AppliedStatus(
                True,
                "applied" if advanced else "stale",
                organisation_id=call.organisation_id,
                incident_id=call.incident_id,
                status=call.status,
            )

    @staticmethod
    async def _insert_deduped(
        session: AsyncSession, call: Call, status: CallStatus, key: str, twilio_status: str
    ) -> uuid.UUID | None:
        return await session.scalar(
            pg_insert(CallEvent)
            .values(
                event_row(
                    organisation_id=call.organisation_id,
                    incident_id=call.incident_id,
                    call_id=call.id,
                    event_type=STATUS_EVENT_MAP[status],
                    provider_event_key=key,
                    data={"provider_status": twilio_status},
                )
            )
            .on_conflict_do_nothing(
                index_elements=["call_id", "provider_event_key"],
                index_where=CallEvent.provider_event_key.is_not(None),
            )
            .returning(CallEvent.id)
        )

    # ------------------------------------------------------------------ gather (contacts)

    async def apply_gather_response(
        self, call_id: uuid.UUID, *, provider_sid: str, digits: str
    ) -> AppliedStatus:
        response = GATHER_RESPONSES.get(digits.strip())
        async with self._session_factory() as session:
            uow = self._uow(session)
            call = await self._locked_call(session, call_id)
            if call is None:
                return AppliedStatus(False, "unknown_call")
            if call.provider_call_sid and call.provider_call_sid != provider_sid:
                return AppliedStatus(False, "sid_mismatch", organisation_id=call.organisation_id)
            key = f"gather:{provider_sid}"
            inserted = await session.scalar(
                pg_insert(CallEvent)
                .values(
                    event_row(
                        organisation_id=call.organisation_id,
                        incident_id=call.incident_id,
                        call_id=call.id,
                        event_type=CallEventType.USER_RESPONSE_RECEIVED,
                        provider_event_key=key,
                        data={"response": response.value if response else None, "digits_len": 1},
                    )
                )
                .on_conflict_do_nothing(
                    index_elements=["call_id", "provider_event_key"],
                    index_where=CallEvent.provider_event_key.is_not(None),
                )
                .returning(CallEvent.id)
            )
            if inserted is None:
                await session.commit()
                return AppliedStatus(
                    True, "duplicate", organisation_id=call.organisation_id, status=call.status
                )
            if response is not None:
                call.structured_response = response
                if response is StructuredCallResponse.CAN_RESPOND:
                    call.acknowledged = True
                if response is StructuredCallResponse.REQUEST_OPERATOR:
                    # A person explicitly asked for an operator: deterministic fail-safe.
                    incident = await IncidentEngine.lock(
                        session, call.organisation_id, call.incident_id
                    )
                    if incident.is_active:
                        await scheduler.ensure_operator_alert_now(session, incident)
                        IncidentEngine.append_event(
                            session,
                            incident,
                            IncidentEventType.OPERATOR_ESCALATION_REQUESTED,
                            "Trusted contact asked for a CareOS operator",
                            actor_type=ActorType.PROVIDER,
                            data={"call_id": str(call.id)},
                        )
            self._notify(uow, call, "CALL_RESPONSE")
            await uow.commit()
            return AppliedStatus(
                True,
                "applied",
                organisation_id=call.organisation_id,
                incident_id=call.incident_id,
                status=call.status,
            )

    # ------------------------------------------------------------------ media / AI session

    async def attach_media_stream(self, raw_token: str) -> MediaContext | None:
        """Single use: the digest match consumes the token. Returns None for anything
        invalid — an unauthenticated stream never learns why."""
        digest = media_token_digest(raw_token)
        async with self._session_factory() as session:
            uow = self._uow(session)
            call = await session.scalar(
                select(Call).where(Call.media_token_digest == digest).with_for_update()
            )
            if (
                call is None
                or call.media_connected_at is not None
                or call.status in TERMINAL_CALL_STATUSES
            ):
                return None
            call.media_connected_at = utcnow()
            trigger_type = await session.scalar(
                select(Incident.trigger_type).where(Incident.id == call.incident_id)
            )
            preferred_name = "there"
            if call.service_user_id is not None:
                names = (
                    await session.execute(
                        select(ServiceUser.preferred_name, ServiceUser.first_name).where(
                            ServiceUser.id == call.service_user_id
                        )
                    )
                ).first()
                if names is not None:
                    preferred_name = names[0] or names[1] or preferred_name
            await session.execute(
                pg_insert(CallEvent).values(
                    event_row(
                        organisation_id=call.organisation_id,
                        incident_id=call.incident_id,
                        call_id=call.id,
                        event_type=CallEventType.MEDIA_STREAM_CONNECTED,
                    )
                )
            )
            self._notify(uow, call, "CALL_STATUS")
            context = MediaContext(
                organisation_id=call.organisation_id,
                incident_id=call.incident_id,
                call_id=call.id,
                preferred_name=preferred_name,
                language="en-GB",
                trigger_type=trigger_type or "SOS_BUTTON",
            )
            await uow.commit()
            return context

    async def start_ai_session(self, context: MediaContext, *, provider: str) -> uuid.UUID:
        async with self._session_factory() as session:
            uow = self._uow(session)
            ai_session = AISession(
                id=uuid.uuid4(),
                organisation_id=context.organisation_id,
                incident_id=context.incident_id,
                call_id=context.call_id,
                purpose=AISessionPurpose.AUTOMATED_VOICE_CALL,
                provider=provider,
                status=AISessionStatus.STARTED,
                started_at=utcnow(),
            )
            session.add(ai_session)
            await session.execute(
                pg_insert(CallEvent).values(
                    event_row(
                        organisation_id=context.organisation_id,
                        incident_id=context.incident_id,
                        call_id=context.call_id,
                        event_type=CallEventType.AI_SESSION_STARTED,
                        data={"provider": provider},
                    )
                )
            )
            incident = await IncidentEngine.lock(
                session, context.organisation_id, context.incident_id
            )
            IncidentEngine.append_event(
                session,
                incident,
                IncidentEventType.AI_CALL_STARTED,
                "AI voice assistant joined the call (advisory only)",
                actor_type=ActorType.AI,
                data={"ai_session_id": str(ai_session.id), "call_id": str(context.call_id)},
            )
            uow.notify(
                RealtimeMessage(
                    type=RealtimeMessageType.INCIDENT_UPDATED,
                    organisation_id=context.organisation_id,
                    incident_id=context.incident_id,
                    payload={"event_type": "AI_CALL_STARTED"},
                )
            )
            await uow.commit()
            return ai_session.id

    async def finish_ai_session(
        self,
        ai_session_id: uuid.UUID,
        context: MediaContext,
        *,
        result: VoiceResult | None,
        failure_reason: str | None = None,
        timed_out: bool = False,
    ) -> None:
        """Persist the advisory outcome. Never touches Incident.status/priority (ADR-016)."""
        async with self._session_factory() as session:
            uow = self._uow(session)
            stored = await session.get(AISession, ai_session_id)
            call = await self._locked_call(session, context.call_id)
            now = utcnow()
            if stored is not None and stored.ended_at is None:
                stored.ended_at = now
                if result is not None:
                    stored.status = AISessionStatus.COMPLETED
                    stored.advisory_summary = result.summary
                    stored.contact_established = result.contact_established
                    stored.requested_human_help = result.requested_human_help
                    stored.urgency_signal = result.urgency_signal
                    stored.language = result.language
                else:
                    stored.status = (
                        AISessionStatus.TIMED_OUT if timed_out else AISessionStatus.FAILED
                    )
                    stored.failure_reason = (failure_reason or "unknown")[:255]
            elif stored is not None:
                await session.commit()
                return  # already finalised (duplicate stop/disconnect)
            ok = result is not None
            if call is not None and result is not None and result.requested_human_help:
                # The person explicitly asked for help: deterministic acknowledgement.
                call.acknowledged = True
            if call is not None:
                await session.execute(
                    pg_insert(CallEvent).values(
                        event_row(
                            organisation_id=call.organisation_id,
                            incident_id=call.incident_id,
                            call_id=call.id,
                            event_type=CallEventType.AI_SESSION_COMPLETED
                            if ok
                            else CallEventType.AI_SESSION_FAILED,
                            data={
                                "urgency_signal": result.urgency_signal.value if result else None,
                                "failure_reason": failure_reason,
                            },
                        )
                    )
                )
            incident = await IncidentEngine.lock(
                session, context.organisation_id, context.incident_id
            )
            if ok and result is not None:
                IncidentEngine.append_event(
                    session,
                    incident,
                    IncidentEventType.AI_CALL_COMPLETED,
                    "AI voice assistant finished (advisory note attached)",
                    actor_type=ActorType.AI,
                    data={
                        "ai_session_id": str(ai_session_id),
                        "advisory": True,
                        "urgency_signal": result.urgency_signal.value,
                        "requested_human_help": result.requested_human_help,
                        "contact_established": result.contact_established,
                        "advisory_summary": result.summary,
                    },
                )
            else:
                IncidentEngine.append_event(
                    session,
                    incident,
                    IncidentEventType.AI_CALL_FAILED,
                    "AI voice assistant unavailable; deterministic escalation continues",
                    actor_type=ActorType.AI,
                    data={"ai_session_id": str(ai_session_id), "error": failure_reason},
                )
            uow.notify(
                RealtimeMessage(
                    type=RealtimeMessageType.INCIDENT_UPDATED,
                    organisation_id=context.organisation_id,
                    incident_id=context.incident_id,
                    payload={"event_type": "AI_RESULT"},
                )
            )
            await uow.commit()

    async def record_dtmf(self, context: MediaContext, digit: str) -> None:
        digit = digit.strip()[:1]
        if not digit:
            return
        async with self._session_factory() as session:
            uow = self._uow(session)
            call = await self._locked_call(session, context.call_id)
            if call is None:
                return
            if digit == "1":
                # "Press 1 if you need help": explicit human keypad action, never AI judgement.
                call.acknowledged = True
            await session.execute(
                pg_insert(CallEvent).values(
                    event_row(
                        organisation_id=call.organisation_id,
                        incident_id=call.incident_id,
                        call_id=call.id,
                        event_type=CallEventType.DTMF_RECEIVED,
                        data={"digit": digit},
                    )
                )
            )
            self._notify(uow, call, "CALL_STATUS")
            await uow.commit()
