"""RunManager: in-process asyncio worker that advances one run at a time (ARCHITECTURE §4.2).

Runs start only on an explicit request (`POST /runs/{id}/start`, or an approval
decision). Nothing is resumed automatically after a restart, so no GPT call is ever
made without a user action.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading

from app.orchestrator.loop import Orchestrator

logger = logging.getLogger("devpilot.runs")


class RunManager:
    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orchestrator = orchestrator
        self._queue: asyncio.Queue[str] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self.current: str | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._worker(), name="devpilot-run-worker")

    async def stop(self) -> None:
        self._orchestrator.request_stop()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._loop = None

    def submit(self, run_id: str) -> bool:
        """Queue a run (thread-safe). A run already waiting in the queue is not queued twice."""
        if self._loop is None or self._queue is None:
            logger.warning("run worker is not running; run %s not queued", run_id)
            return False
        with self._lock:
            if run_id in self._pending:
                return True
            self._pending.add(run_id)
        self._loop.call_soon_threadsafe(self._queue.put_nowait, run_id)
        return True

    async def join(self) -> None:
        """Wait until every queued run has been advanced (tests and the demo)."""
        if self._queue is not None:
            await asyncio.sleep(0)  # let pending call_soon_threadsafe puts land in the queue first
            await self._queue.join()

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            run_id = await self._queue.get()
            with self._lock:
                self._pending.discard(run_id)
            self.current = run_id
            try:
                await asyncio.to_thread(self._orchestrator.advance, run_id)
            except Exception:
                logger.exception("run worker failed on %s", run_id)
            finally:
                self.current = None
                self._queue.task_done()
