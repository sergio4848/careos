"""Unit of Work: one transaction; side effects released only after it commits.

Transaction boundary rules (ADR-012):

* Everything that must be true for safety (incident, timeline, audit, escalation schedule)
  is written in the *same* database transaction.
* Realtime notifications (:meth:`notify`) and after-commit hooks (:meth:`after_commit`, used
  for metrics and "accepted" logs) run only after a successful commit and are discarded on
  rollback or commit failure, so nothing describes state that was never persisted.
* After-commit work is best-effort and bounded: it can never raise into the caller, never
  delay the response beyond ``publish_timeout_seconds`` and never undo the commit. A lost
  notification is recovered by the console's REST reconciliation (ADR-008).
* Provider work (calls, notifications) is never done here: it is persisted as
  ``scheduled_actions`` in the transaction and executed by the worker (the outbox).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from careos.contracts.realtime import RealtimeMessage, RealtimePublisher
from careos.core.logging import get_logger
from careos.core.metrics import REALTIME_DELIVERY_FAILURES

log = get_logger(__name__)

DEFAULT_PUBLISH_TIMEOUT_SECONDS = 1.0


class UnitOfWork:
    def __init__(
        self,
        session: AsyncSession,
        publisher: RealtimePublisher,
        *,
        publish_timeout_seconds: float = DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    ) -> None:
        self.session = session
        self._publisher = publisher
        self._publish_timeout = publish_timeout_seconds
        self._pending: list[RealtimeMessage] = []
        self._hooks: list[Callable[[], None]] = []

    def notify(self, message: RealtimeMessage) -> None:
        self._pending.append(message)

    def after_commit(self, hook: Callable[[], None]) -> None:
        """Run ``hook`` once the transaction has committed (never on rollback)."""
        self._hooks.append(hook)

    def discard_pending(self) -> None:
        self._pending.clear()
        self._hooks.clear()

    async def commit(self) -> None:
        try:
            await self.session.commit()
        except BaseException:
            self.discard_pending()
            raise
        pending, self._pending = self._pending, []
        hooks, self._hooks = self._hooks, []
        for hook in hooks:
            try:
                hook()
            except Exception as exc:
                log.error("uow.after_commit_hook_failed", failure_category=type(exc).__name__)
        for message in pending:
            await self._publish(message)

    async def rollback(self) -> None:
        self.discard_pending()
        await self.session.rollback()

    async def _publish(self, message: RealtimeMessage) -> None:
        try:
            await asyncio.wait_for(self._publisher.publish(message), self._publish_timeout)
        except Exception as exc:
            REALTIME_DELIVERY_FAILURES.labels(stage="after_commit").inc()
            log.warning(
                "realtime.after_commit_publish_failed",
                failure_category=type(exc).__name__,
                message_type=message.type.value,
                organisation_id=str(message.organisation_id),
                incident_id=str(message.incident_id) if message.incident_id else None,
            )
