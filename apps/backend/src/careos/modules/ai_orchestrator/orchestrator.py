"""AI orchestration — assistive, optional and never safety-critical (ADR-006).

Guarantees enforced here:
* every provider call is bounded by a timeout;
* the orchestrator never raises: failures become an ``AIOutcome`` with ``ok=False``;
* outputs are advisory text only. There is no API through which AI output can change
  incident status, priority, assignment or resolution.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Protocol

from careos.core.logging import get_logger
from careos.core.metrics import AI_TIMEOUTS, PROVIDER_CALLS, PROVIDER_FAILURES

log = get_logger(__name__)

ADVISORY_DISCLAIMER = "AI-generated advisory note. Not a clinical assessment; verify with a human."


class AIProviderError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CheckInContext:
    organisation_id: uuid.UUID
    incident_id: uuid.UUID
    trigger_type: str
    language: str = "en-GB"


@dataclass(frozen=True, slots=True)
class CheckInAssist:
    advisory_summary: str
    model: str | None = None


class AIProvider(Protocol):
    name: str

    async def assist_check_in(self, context: CheckInContext) -> CheckInAssist: ...


class MockAIProvider:
    name = "mock-ai"

    async def assist_check_in(self, context: CheckInContext) -> CheckInAssist:
        return CheckInAssist(
            advisory_summary=(
                "Automated check-in assistant did not capture a verbal response. "
                f"{ADVISORY_DISCLAIMER}"
            ),
            model="mock-assist-1",
        )


class UnavailableAIProvider:
    """Simulates an AI outage to demonstrate that incident handling is unaffected."""

    name = "mock-ai-unavailable"

    async def assist_check_in(self, context: CheckInContext) -> CheckInAssist:
        raise AIProviderError("AI provider unavailable")


@dataclass(frozen=True, slots=True)
class AIOutcome:
    ok: bool
    provider: str
    timed_out: bool = False
    advisory_summary: str | None = None
    model: str | None = None
    error: str | None = None


class AIOrchestrator:
    def __init__(self, provider: AIProvider | None, timeout_seconds: float) -> None:
        self._provider = provider
        self._timeout = timeout_seconds

    @property
    def enabled(self) -> bool:
        return self._provider is not None

    @property
    def provider_name(self) -> str:
        return self._provider.name if self._provider else "disabled"

    async def assist_check_in(self, context: CheckInContext) -> AIOutcome:
        if self._provider is None:
            return AIOutcome(ok=False, provider="disabled", error="ai_disabled")
        name = self._provider.name
        try:
            async with asyncio.timeout(self._timeout):
                result = await self._provider.assist_check_in(context)
        except TimeoutError:
            PROVIDER_CALLS.labels("ai", name, "timeout").inc()
            PROVIDER_FAILURES.labels("ai", name, "timeout").inc()
            AI_TIMEOUTS.labels(name).inc()
            log.warning(
                "ai.check_in_failed",
                provider=name,
                failure_category="timeout",
                incident_id=str(context.incident_id),
                organisation_id=str(context.organisation_id),
            )
            return AIOutcome(ok=False, provider=name, timed_out=True, error="timeout")
        except Exception as exc:  # isolate ANY AI failure from the incident workflow
            PROVIDER_CALLS.labels("ai", name, "error").inc()
            PROVIDER_FAILURES.labels("ai", name, "provider_error").inc()
            log.warning(
                "ai.check_in_failed",
                provider=name,
                failure_category="provider_error",
                error_type=type(exc).__name__,
                incident_id=str(context.incident_id),
                organisation_id=str(context.organisation_id),
            )
            return AIOutcome(ok=False, provider=name, error=type(exc).__name__)
        PROVIDER_CALLS.labels("ai", name, "ok").inc()
        return AIOutcome(
            ok=True, provider=name, advisory_summary=result.advisory_summary, model=result.model
        )
