"""In-process queue manager (ARCHITECTURE.md §15–17, §21).

The database is the source of truth for job state. One backend process owns
the queue: a lifespan task claims queued jobs up to the configured
concurrency and runs them. Restart recovery marks previously active jobs as
``interrupted_by_restart`` so nothing stays ``downloading`` forever.
"""

import asyncio
import threading
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories import activity
from app.repositories import jobs as job_repo
from app.services.job_mapping import job_to_dto
from app.services.job_runner import ProcessRegistry, RunnerDeps, run_download_job
from app.utils.processes import ensure_process_terminated

_CANCEL_GRACE_SECONDS = 5.0


class QueueManager:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        deps: RunnerDeps,
        concurrency: int,
        poll_interval_seconds: float,
    ) -> None:
        self._session_factory = session_factory
        self._deps = deps
        self._concurrency = max(1, concurrency)
        self._poll_interval = poll_interval_seconds
        self._loop_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: ProcessRegistry = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._cancel_gates: dict[str, threading.Lock] = {}
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("queue manager already started")
        self._started = True
        await self._recover_interrupted_jobs()
        self._loop_task = asyncio.create_task(self._loop(), name="queue-manager")

    async def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        self._wake_event.set()
        if self._loop_task is not None:
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._cancel_gates.clear()
        self._started = False

    def notify(self) -> None:
        """Wake the claim loop because a new job was queued."""
        self._wake_event.set()

    def publish_snapshot(self, event_name: str, record: Any) -> None:
        self._deps.bus.publish(
            {
                "event": event_name,
                "id": None,
                "data": job_to_dto(record).model_dump(mode="json"),
            }
        )

    def cancel_event_for(self, job_id: str) -> asyncio.Event:
        return self._cancel_events.setdefault(job_id, asyncio.Event())

    async def request_cancel(self, job_id: str) -> None:
        """Signal cancellation independent of any in-memory job snapshot.

        The per-job gate linearizes this signal against the storage layer's
        atomic publish decision: either cancellation is observed before the
        final file becomes visible, or the publish has already won.
        """
        cancel_event = self.cancel_event_for(job_id)
        gate = self._cancel_gates.get(job_id)
        if gate is not None:
            with gate:
                cancel_event.set()
        else:
            cancel_event.set()
        process = self._processes.get(job_id)
        if process is not None and process.returncode is None:
            await ensure_process_terminated(process, _CANCEL_GRACE_SECONDS)

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._claim_and_run()
            except Exception:
                if self._stop_event.is_set():
                    break
                raise
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=self._poll_interval
                )
            except TimeoutError:
                pass
            self._wake_event.clear()

    async def _claim_and_run(self) -> None:
        self._tasks = {job_id: task for job_id, task in self._tasks.items() if not task.done()}
        available = self._concurrency - len(self._tasks)
        if available <= 0:
            return
        async with self._session_factory() as session:
            queued = await job_repo.claimable_queued_jobs(session, limit=available)
        for record in queued:
            if record.id in self._tasks or self._stop_event.is_set():
                continue
            cancel_event = self.cancel_event_for(record.id)
            self._tasks[record.id] = asyncio.create_task(
                run_download_job(
                    record.id,
                    deps=self._deps,
                    cancel_event=cancel_event,
                    processes=self._processes,
                    cancel_gates=self._cancel_gates,
                ),
                name=f"job-{record.id}",
            )

    async def _recover_interrupted_jobs(self) -> None:
        async with self._session_factory() as session:
            interrupted_ids = await job_repo.mark_interrupted_active_jobs(session)
            for job_id in interrupted_ids:
                await activity.add_activity_event(
                    session,
                    job_id=job_id,
                    level="error",
                    event_type="job",
                    message="Marked interrupted by application restart",
                )
            await session.commit()
        for job_id in interrupted_ids:
            async with self._session_factory() as session:
                record = await job_repo.get_job_any_owner(session, job_id)
            if record is not None:
                self._deps.bus.publish(
                    {
                        "event": "job.failed",
                        "id": None,
                        "data": job_to_dto(record).model_dump(mode="json"),
                    }
                )
