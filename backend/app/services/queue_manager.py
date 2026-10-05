"""In-process queue manager (ARCHITECTURE.md §15–17, §21).

The database is the source of truth for job state. One backend process owns
the queue: a lifespan task claims queued jobs up to the configured
concurrency and runs them. Restart recovery marks previously active jobs as
``interrupted_by_restart`` so nothing stays ``downloading`` forever.
"""

import asyncio
import threading
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Any
from weakref import WeakValueDictionary

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.jobs import ACTIVE_JOB_STATUSES
from app.models.jobs import DownloadJob
from app.repositories import activity
from app.repositories import jobs as job_repo
from app.services.collection_child_runner import ChildRunnerDeps
from app.services.collection_parent_runner import run_collection_parent
from app.services.collection_persistence import (
    CollectionRecoveryOutcome,
    stage_collection_startup_recovery,
)
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
        collection_deps: ChildRunnerDeps | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._deps = deps
        self._collection_deps = collection_deps
        self._concurrency = max(1, concurrency)
        self._poll_interval = poll_interval_seconds
        self._loop_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: ProcessRegistry = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._cancel_gates: dict[str, threading.Lock] = {}
        self._lifecycle_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._started = False
        self._stop_task: asyncio.Task[None] | None = None
        self._operations: set[asyncio.Future[Any]] = set()

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("queue manager already started")
        if self._stop_task is not None and not self._stop_task.done():
            raise RuntimeError("queue manager is stopping")
        self._stop_task = None
        self._stop_event.clear()
        self._started = True
        try:
            await self._recover_interrupted_jobs()
        except BaseException:
            self._started = False
            raise
        self._loop_task = asyncio.create_task(self._loop(), name="queue-manager")

    async def stop(self) -> None:
        """Coalesce callers; their cancellation never abandons the owned stop.

        Lifespan closes request admission before stop. Already entered lifecycle
        operations (including lock waiters/disconnected shield owners) register
        before their first await; stop never takes their lifecycle locks.
        """
        operation = self._stop_task
        if operation is not None and operation.done() and (
            operation.exception() is not None or self._tasks or self._operations
        ):
            # Failed settlement is retryable. Unstarted/stopped test runtimes can
            # also accept API work while the DB is still open; drain that too.
            operation = None
        if operation is None:
            if not self._started and not self._tasks and not self._operations:
                return
            self._stop_event.set()
            self._wake_event.set()
            operation = asyncio.create_task(self._stop(), name="queue-manager-stop")
            self._stop_task = operation
        cancelled = False
        while True:
            try:
                await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                if operation.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    def own_operation[T](self, operation: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Register shield-owned work synchronously, before it can run or await.

        Lifecycle reads need this too: a cancelled caller can leave the read's
        session close running. Completion is the task's definite return, never
        its caller's disconnect. Retrieve detached outcomes without changing
        what a still-awaiting caller receives.
        """
        task = asyncio.create_task(operation)
        self._operations.add(task)

        def finished(done: asyncio.Task[T]) -> None:
            self._operations.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return task

    @asynccontextmanager
    async def runtime_operation(self) -> AsyncIterator[None]:
        """Track the whole API lifecycle, before lock acquisition until finalization.

        A completion token, not the caller task's cancellation status, records
        quiescence. Existing definite collection operations retain this scope
        until their owned transaction/close/publication has actually returned.
        Registration/removal are synchronous on the owning event loop.
        """
        finished = asyncio.get_running_loop().create_future()
        self._operations.add(finished)
        try:
            yield
        finally:
            finished.set_result(None)
            self._operations.remove(finished)

    async def _stop(self) -> None:
        loop_error: BaseException | None = None
        if self._loop_task is not None:
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                loop_error = exc
        # Snapshot: no mutation-size race, no synthetic user Cancel event/marker.
        # Each attempt receives at most one cancellation from this shared stop.
        attempts = tuple(self._tasks.values())
        for task in attempts:
            if not task.done():
                task.cancel()
        await asyncio.gather(*attempts, return_exceptions=True)
        # API holders may be waiting for those attempts. Drain without any SQL
        # writer or lifecycle lock, and include waiters registered during drain.
        while self._operations:
            await asyncio.gather(*tuple(self._operations), return_exceptions=True)
        if loop_error is not None:
            raise loop_error
        recovered_ids = await self._recover_collections()
        await self._publish_recovered(recovered_ids)
        # Failure above leaves registries/started intact and durable recovery
        # available. Clear only after all close/publication work is definite.
        self._loop_task = None
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

    def lifecycle_lock(self, job_id: str) -> asyncio.Lock:
        """Serialize retry/cancel without retaining inactive or unknown IDs.

        Holders and waiters keep strong references (including ``async with``),
        so every overlapping operation uses the same lock. Lookup/allocation
        has no await and runs on the single queue event loop.
        """
        return self._lifecycle_locks.setdefault(job_id, asyncio.Lock())

    async def wait_for_attempt(self, job_id: str, *, retrieve_outcome: bool = False) -> None:
        task = self._tasks.get(job_id)
        if task is not None:
            # A disconnected retry request must not cancel the old runner.
            if retrieve_outcome:
                # Collection Cancel settles from durable child facts, not the
                # drained attempt's cancellation or mapped failure outcome.
                await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            else:
                await asyncio.shield(task)

    def reset_attempt(self, job_id: str) -> None:
        """Only after old execution and its helpers have fully unwound."""
        self._cancel_events.pop(job_id, None)
        self._cancel_gates.pop(job_id, None)
        self._processes.pop(job_id, None)

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

    async def request_collection_cancel(self, job_id: str) -> None:
        """Commit-first caller owns this signal and its gate-waiter thread.

        Acquire in a worker, set the asyncio event on its loop while holding
        the publication gate. Never block the event loop on a storage thread.
        Single cancellation keeps its existing entry point unchanged.
        """
        gate = self._cancel_gates.get(job_id)
        if gate is not None:
            waiter = asyncio.create_task(asyncio.to_thread(gate.acquire))
            cancelled = False
            while True:
                try:
                    await asyncio.shield(waiter)
                    break
                except asyncio.CancelledError:
                    cancelled = True
            try:
                self.cancel_event_for(job_id).set()
            finally:
                gate.release()
            if cancelled:
                raise asyncio.CancelledError
        else:
            self.cancel_event_for(job_id).set()
        # The owned child runner reacts to the event and owns process reap,
        # spawn races and helper drain; never release/reset its slot here.

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._claim_and_run()
            except Exception:
                if self._stop_event.is_set():
                    break
                raise
            if self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=self._poll_interval
                )
            except TimeoutError:
                pass
            self._wake_event.clear()

    async def _claim_and_run(self) -> None:
        if self._stop_event.is_set():
            return
        self._tasks = {job_id: task for job_id, task in self._tasks.items() if not task.done()}
        available = self._concurrency - len(self._tasks)
        if available <= 0:
            return
        async with self._session_factory() as session:
            if self._collection_deps is None:
                # Direct legacy/test runtimes remain single-only; filter BEFORE
                # LIMIT rather than starving their supported execution path.
                queued = await job_repo.claimable_queued_jobs(
                    session, limit=available, include_collections=False,
                )
            else:
                queued = await job_repo.claimable_queued_jobs(session, limit=available)
        for record in queued:
            if record.id in self._tasks or self._stop_event.is_set():
                continue
            lock = self._lifecycle_locks.get(record.id)
            if lock is not None and lock.locked():
                # Retry's commit may already be visible while its await is
                # returning. Do not capture the old cancel event before reset.
                continue
            if record.job_type == "collection":
                if self._collection_deps is None:
                    continue
                cancel_event = self.cancel_event_for(record.id)
                gate = self._cancel_gates.setdefault(record.id, threading.Lock())
                self._tasks[record.id] = asyncio.create_task(
                    self._run_collection(record.id, record.owner_id, cancel_event, gate),
                    name=f"collection-{record.id}",
                )
                continue
            if record.job_type != "single":
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

    async def _run_collection(
        self, job_id: str, owner_id: str, cancel_event: asyncio.Event,
        gate: threading.Lock,
    ) -> None:
        assert self._collection_deps is not None
        await run_collection_parent(
            job_id, owner_id, deps=self._collection_deps,
            cancel_event=cancel_event, processes=self._processes,
            cancel_gate=gate, bus=self._deps.bus,
        )

    async def _recover_collections(self) -> list[str]:
        # Startup before claims OR orderly stop AFTER the full owned drain:
        # no old live work exists. Candidates carry identity, never decisions.
        async with self._session_factory() as session:
            candidates = (await session.execute(select(DownloadJob.id, DownloadJob.owner_id)
                .where(DownloadJob.job_type == "collection", or_(
                    DownloadJob.status.in_([s.value for s in ACTIVE_JOB_STATUSES]),
                    (DownloadJob.status == "queued")
                    & DownloadJob.collection_cancel_requested_at.is_not(None),
                )))).all()
        recovered_ids = []
        for job_id, owner_id in candidates:
            async with self._session_factory() as session:
                try:
                    outcome = await stage_collection_startup_recovery(
                        session, job_id=job_id, owner_id=owner_id)
                    if outcome == CollectionRecoveryOutcome.STAGED:
                        await session.commit()
                        recovered_ids.append(job_id)
                    else:
                        await session.rollback()
                except BaseException:
                    await session.rollback()
                    raise
        return recovered_ids

    async def _recover_interrupted_jobs(self) -> None:
        recovered_ids = await self._recover_collections()
        # Collection commits are independently atomic/idempotent. This legacy
        # single recovery is startup-only, never part of orderly stop.
        async with self._session_factory() as session:
            try:
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
            except BaseException:
                await session.rollback()
                raise
        await self._publish_recovered([*recovered_ids, *interrupted_ids])

    async def _publish_recovered(self, recovered_ids: list[str]) -> None:
        for job_id in recovered_ids:
            async with self._session_factory() as session:
                record = await job_repo.get_job_any_owner(session, job_id)
            if record is not None:
                self._deps.bus.publish(
                    {
                        "event": f"job.{record.status}",
                        "id": None,
                        "data": job_to_dto(record).model_dump(mode="json"),
                    }
                )
