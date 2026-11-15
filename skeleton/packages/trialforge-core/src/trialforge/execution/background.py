"""Optional in-process background execution (no external queue required).

Work is durable because the service persists jobs/state in its repository; this runner only supplies
asyncio tasks. After a restart, call ``service.recover()`` and submit the experiment again.
"""

from __future__ import annotations

import asyncio

from trialforge.models.core import ExperimentResult


class LocalBackgroundRunner:
    """Run experiments as asyncio tasks inside the current event loop."""

    def __init__(self, service) -> None:
        self.service = service
        self._tasks: dict[str, asyncio.Task] = {}

    def submit(self, experiment_id: str) -> asyncio.Task:
        """Start executing pending runs; returns immediately. Re-submitting a running experiment is a no-op."""
        task = self._tasks.get(experiment_id)
        if task and not task.done():
            return task
        task = asyncio.create_task(self.service.run_pending(experiment_id))
        self._tasks[experiment_id] = task
        return task

    def is_running(self, experiment_id: str) -> bool:
        """True while a submitted experiment's task is still executing."""
        t = self._tasks.get(experiment_id)
        return bool(t and not t.done())

    async def wait(self, experiment_id: str) -> ExperimentResult:
        """Await a submitted experiment and return its result."""
        return await self._tasks[experiment_id]

    async def cancel(self, experiment_id: str) -> ExperimentResult:
        """Request cancellation through the service (marks runs CANCELLED), then await the task."""
        result = await self.service.cancel(experiment_id)
        t = self._tasks.get(experiment_id)
        if t:
            try:
                await t
            except asyncio.CancelledError:
                pass
        return result

    async def shutdown(self) -> None:
        """Cancel unfinished tasks. Persisted state stays consistent; use ``service.recover()`` after restart."""
        for t in self._tasks.values():
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
