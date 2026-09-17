"""Unit of Work: one transaction + realtime messages released only after commit.

Services add realtime notifications with :meth:`UnitOfWork.notify`. They are published
*after* a successful commit, so consoles never see state that was rolled back.
Publishing is best-effort by design (see ADR-008).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from careos.contracts.realtime import RealtimeMessage, RealtimePublisher


class UnitOfWork:
    def __init__(self, session: AsyncSession, publisher: RealtimePublisher) -> None:
        self.session = session
        self._publisher = publisher
        self._pending: list[RealtimeMessage] = []

    def notify(self, message: RealtimeMessage) -> None:
        self._pending.append(message)

    async def commit(self) -> None:
        await self.session.commit()
        pending, self._pending = self._pending, []
        for message in pending:
            await self._publisher.publish(message)

    async def rollback(self) -> None:
        self._pending.clear()
        await self.session.rollback()
