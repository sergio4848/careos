from __future__ import annotations

import asyncio
import uuid

import pytest

from careos.core.errors import ValidationFailedError
from careos.core.rate_limit import InMemoryRateLimiter
from careos.modules.ai_orchestrator.orchestrator import (
    AIOrchestrator,
    CheckInAssist,
    CheckInContext,
    MockAIProvider,
    UnavailableAIProvider,
)
from careos.modules.escalation_engine.models import EscalationActionType as A
from careos.modules.escalation_engine.policies import validate_steps
from careos.modules.escalation_engine.schemas import EscalationStepWrite as Step

CTX = CheckInContext(organisation_id=uuid.uuid4(), incident_id=uuid.uuid4(), trigger_type="SOS")


def test_policy_must_end_with_human_operators() -> None:
    with pytest.raises(ValidationFailedError, match="OPERATOR_ESCALATION"):
        validate_steps([Step(delay_seconds=0, action_type=A.AUTOMATED_USER_CONTACT)])


def test_policy_delays_must_not_decrease() -> None:
    with pytest.raises(ValidationFailedError, match="non-decreasing"):
        validate_steps(
            [
                Step(delay_seconds=60, action_type=A.CALL_TRUSTED_CONTACT, contact_priority=1),
                Step(delay_seconds=30, action_type=A.OPERATOR_ESCALATION),
            ]
        )


def test_contact_steps_need_priority() -> None:
    with pytest.raises(ValidationFailedError, match="contact_priority"):
        validate_steps(
            [
                Step(delay_seconds=0, action_type=A.CALL_TRUSTED_CONTACT),
                Step(delay_seconds=30, action_type=A.OPERATOR_ESCALATION),
            ]
        )


def test_valid_policy() -> None:
    validate_steps(
        [
            Step(delay_seconds=0, action_type=A.AUTOMATED_USER_CONTACT),
            Step(delay_seconds=30, action_type=A.CALL_TRUSTED_CONTACT, contact_priority=1),
            Step(delay_seconds=90, action_type=A.OPERATOR_ESCALATION),
        ]
    )


async def test_ai_orchestrator_isolates_failures() -> None:
    outcome = await AIOrchestrator(UnavailableAIProvider(), timeout_seconds=1).assist_check_in(CTX)
    assert not outcome.ok and outcome.error == "AIProviderError"


async def test_ai_orchestrator_enforces_timeout() -> None:
    class SlowProvider:
        name = "slow"

        async def assist_check_in(self, context: CheckInContext) -> CheckInAssist:
            await asyncio.sleep(5)
            return CheckInAssist("never")

    outcome = await AIOrchestrator(SlowProvider(), timeout_seconds=0.05).assist_check_in(CTX)
    assert not outcome.ok and outcome.timed_out


async def test_ai_disabled_and_mock() -> None:
    assert not AIOrchestrator(None, timeout_seconds=1).enabled
    outcome = await AIOrchestrator(MockAIProvider(), timeout_seconds=1).assist_check_in(CTX)
    assert (
        outcome.ok and outcome.advisory_summary and "advisory" in outcome.advisory_summary.lower()
    )


async def test_in_memory_rate_limiter() -> None:
    limiter = InMemoryRateLimiter()
    results = [await limiter.hit("k", limit=3, window_seconds=60) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    await limiter.reset("k")
    assert (await limiter.hit("k", limit=3, window_seconds=60)).allowed
