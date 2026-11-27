"""Internal one-child execution. No dispatch, parent settlement or reset policy.

Caller owns the active parent attempt, its event/gate/registry and serial child
ordering. Return only after all owned tasks/callbacks are drained. Cancellation
and mapped failures propagate without choosing durable parent/child precedence.
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.jobs import (
    CollectionProfile,
    JobError,
    JobErrorCode,
    JobStatus,
    output_profile_allowed,
)
from app.domain.platforms import detect_platform
from app.integrations.ffmpeg import FFmpegError, FFmpegProcessor
from app.integrations.mutagen_tags import TAGGABLE_SUFFIXES, TagError
from app.integrations.ytdlp import DownloadProgress, YtDlpAnalyzer, YtDlpDownloader, YtDlpError
from app.models.jobs import ActivityEvent, DownloadJob, JobItem
from app.repositories import activity
from app.services.analysis_normalizer import normalize_source_metadata
from app.services.collection_persistence import (
    stage_child_completion,
    stage_child_progress,
    stage_child_transition,
)
from app.services.collection_profile_resolver import resolve_native_child
from app.services.conversion_plan import plan_conversion, resolve_output_profile
from app.services.job_runner import (
    _FFMPEG_KIND_TO_JOB_CODE,
    _REMUX_FALLBACK_SAVED,
    _TAG_FALLBACK_SAVED,
    _YT_DLP_KIND_TO_JOB_CODE,
    _text_tag_editor,
)
from app.services.prepared_media import OriginalRemux, measure_final_dimensions, prepare_media
from app.services.storage_service import PlacementCancelled, StagedEditResult, StorageService
from app.services.text_tags import intended_text_tags

logger = logging.getLogger(__name__)


class ChildResult(StrEnum):
    SKIPPED = "skipped"
    COMPLETED = "completed"


class _LostClaim(Exception):
    pass


def _safe_duration_seconds(value: object) -> float | None:
    """Catalog duration only from real, representable probe metadata.

    A finite nonnegative number survives, including a real zero-length
    item; booleans, non-numbers, negatives, NaN/inf and integers too large
    to convert to float become unknown instead of being truncated into a
    claim (same representation-safety pattern as the analysis normalizer).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    if not isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _tag_source_title(*candidates: object) -> str | None:
    """First candidate the text-tag projection accepts as a source title.

    Uses the projection's own rules (string, trimmed non-blank, no NUL,
    UTF-8 encodable), so an unusable candidate falls through to the next one.
    Only the embedded TITLE fallback is affected; naming/catalog are not.
    """
    for value in candidates:
        if isinstance(value, str) and intended_text_tags(None, source_title=value) is not None:
            return value
    return None


@dataclass(slots=True)
class ChildRunnerDeps:
    session_factory: async_sessionmaker[AsyncSession]
    analyzer: YtDlpAnalyzer
    downloader: YtDlpDownloader
    ffmpeg: FFmpegProcessor
    storage: StorageService


async def run_collection_child(
    job_id: str, owner_id: str, child_id: str, *, deps: ChildRunnerDeps,
    cancel_event: asyncio.Event, processes: dict[str, asyncio.subprocess.Process],
    cancel_gate: threading.Lock, committed_activities: list[ActivityEvent] | None = None,
) -> ChildResult:
    """Claim persisted eligibility before probing; commit each staged transaction.

    The owned task receives cancellation once, even when the caller repeatedly
    cancels while adapters reap subprocesses. Parent event cancellation also
    interrupts a blocked probe/download/merge, including spawn registration races.
    No SSE events are emitted by this internal boundary. ``committed_activities``
    is a per-call caller-owned sink: an Activity row is appended only right
    after its transaction committed, so the caller may publish it even when
    this call later raises (e.g. cancellation or cleanup failure).
    """
    execution = _ChildExecution(job_id, owner_id, child_id, deps, cancel_event,
                                processes, cancel_gate, committed_activities)
    task = asyncio.create_task(execution.run())
    cancel_task: asyncio.Task[None] | None = None

    async def signal_cancel() -> None:
        def signal() -> None:
            # Never wait for the storage thread's gate on the event loop. The
            # publication predicate changes only under that same gate.
            with cancel_gate:
                execution.stopped.set()

        await asyncio.to_thread(signal)
        if not task.done() and not execution.placing:
            task.cancel()

    def cancel() -> None:
        nonlocal cancel_task
        # This loop-local flag prevents catalog work while the signal waits for
        # a winning publish. It is deliberately NOT the storage predicate.
        execution.cancel_pending = True
        if cancel_task is None:
            cancel_task = asyncio.create_task(signal_cancel())

    async def watch() -> None:
        await cancel_event.wait()
        cancel()

    watcher = asyncio.create_task(watch())
    cancellation = None
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = exc
                cancel()
        if cancellation is not None:
            # Retrieve an error without letting it replace caller cancellation.
            if not task.cancelled():
                task.exception()
            raise cancellation
        return task.result()
    except PlacementCancelled:
        # Placement drains through uncancel(); its cancellation can surface at
        # either shield(task) or task.result(). Normalize both owned-task exits.
        raise asyncio.CancelledError from None
    finally:
        watcher.cancel()
        # Watcher contains no asynchronous cleanup; still retrieve its outcome.
        drain = asyncio.gather(watcher, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                cancellation = asyncio.CancelledError()
                cancel()
        # Own the signal helper as well as the execution/storage drain. Repeated
        # caller cancellation must neither cancel its to_thread waiter nor let
        # the attempt return while that thread still owns/waits for the gate.
        if cancel_task is not None:
            while not cancel_task.done():
                try:
                    await asyncio.shield(cancel_task)
                except asyncio.CancelledError:
                    cancellation = asyncio.CancelledError()
            cancel_task.result()
        if cancellation is not None:
            raise cancellation


class _ChildExecution:
    def __init__(self, job_id: str, owner_id: str, child_id: str, deps: ChildRunnerDeps,
                 cancel_event: asyncio.Event, processes: dict[str, asyncio.subprocess.Process],
                 cancel_gate: threading.Lock,
                 committed_activities: list[ActivityEvent] | None = None) -> None:
        self.job_id, self.owner_id, self.child_id = job_id, owner_id, child_id
        self.deps, self.cancel_event = deps, cancel_event
        self.processes, self.cancel_gate = processes, cancel_gate
        self.committed_activities = committed_activities
        self.stopped = threading.Event()
        self.cancel_pending = False
        self.process: asyncio.subprocess.Process | None = None
        self.status = JobStatus.QUEUED
        self.placing = False

    async def check(self) -> None:
        if self.cancel_pending or self.cancel_event.is_set() or self.stopped.is_set():
            raise asyncio.CancelledError

    async def transition(self, status: JobStatus) -> None:
        await self.check()
        async with self.deps.session_factory() as session:
            if not await stage_child_transition(
                session, job_id=self.job_id, owner_id=self.owner_id, child_id=self.child_id,
                expected_status=self.status, status=status,
            ):
                raise _LostClaim
            await session.commit()
        self.status = status

    def register(self, process: asyncio.subprocess.Process) -> asyncio.Task[Any] | None:
        self.process = process
        self.processes[self.job_id] = process
        if self.cancel_pending or self.cancel_event.is_set() or self.stopped.is_set():
            # The adapter owns reap. Kill immediately for cancel-before-registration;
            # the watcher also cancels its owned operation, never a detached helper.
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        return None

    def forwarder(
        self, index: int, count: int,
    ) -> Callable[[DownloadProgress], asyncio.Task[None]]:
        def forward(value: DownloadProgress) -> asyncio.Task[None]:
            async def write() -> None:
                total = value.total_bytes or value.total_bytes_estimate
                fraction = (value.downloaded_bytes or 0) / total if total else 0
                async with self.deps.session_factory() as session:
                    await stage_child_progress(
                        session, job_id=self.job_id, owner_id=self.owner_id,
                        child_id=self.child_id,
                        progress_percent=min(100, max(0, (index + fraction) / count * 100)),
                        downloaded_bytes=value.downloaded_bytes, total_bytes=value.total_bytes,
                        speed_bytes_per_second=value.speed_bytes_per_second,
                        eta_seconds=value.eta_seconds,
                    )
                    await session.commit()
            return asyncio.create_task(write())
        return forward

    async def run(self) -> ChildResult:
        try:
            await self.transition(JobStatus.ANALYZING)
            async with self.deps.session_factory() as session:
                parent, child = (await session.execute(
                    select(DownloadJob, JobItem).join(JobItem, JobItem.job_id == DownloadJob.id)
                    .where(DownloadJob.id == self.job_id, JobItem.id == self.child_id)
                )).one()
            # Parent-level output choice; an unknown target or one outside the
            # collection mode's set fails this child closed before probe.
            # Parent settlement stays with the caller.
            output = resolve_output_profile(parent.output_profile)
            if output is not None and (
                parent.collection_mode not in {"audio", "video"}
                or not output_profile_allowed(parent.collection_mode, output)
            ):
                raise JobError(
                    JobErrorCode.INVALID_PROFILE,
                    detail=f"output profile {output!r} does not fit "
                    f"{parent.collection_mode!r} collections",
                )
            await self.check()
            source = await self.deps.analyzer.probe_child(child.source_url)
            await self.check()
            try:
                profile = (CollectionProfile.model_validate_json(parent.collection_profile_json)
                           if parent.collection_profile_json else None)
            except ValidationError as exc:
                raise JobError(JobErrorCode.INVALID_PROFILE) from exc
            plan = resolve_native_child(source, mode=parent.collection_mode or "", profile=profile)
            # Pure check: an incompatible resolved source fails before the
            # DOWNLOADING claim, staging and any media I/O.
            conversion = (plan_conversion(output, plan.selected, plan.companion)
                          if output is not None else None)
            await self.transition(JobStatus.DOWNLOADING)

            async def before_merge() -> None:
                await self.transition(JobStatus.POST_PROCESSING)

            # Only recorded here; the warning is written with the completion
            # transaction so a rolled back or lost completion leaves none.
            remux_fallback = False

            def record_remux_fallback() -> None:
                nonlocal remux_fallback
                remux_fallback = True

            media = await prepare_media(
                selected=plan.selected, companion=plan.companion, source_url=child.source_url,
                staging_key=child.id, storage=self.deps.storage, downloader=self.deps.downloader,
                ffmpeg=self.deps.ffmpeg, check_cancelled=self.check,
                on_process_started=self.register, progress_forwarder=self.forwarder,
                before_merge=before_merge, child_output=True, output=output,
                original_remux=OriginalRemux(before_merge, record_remux_fallback),
            )
            # Best-effort read-only measurement of the staged video; audio is
            # never probed. Cancellation propagates; the staged file is untouched.
            dimensions = None
            if plan.selected.kind != "audio_only":
                dimensions = await measure_final_dimensions(
                    Path(media), storage=self.deps.storage, staging_key=child.id,
                    ffmpeg=self.deps.ffmpeg, check_cancelled=self.check,
                    on_process_started=self.register,
                )
            # M6.5 text tags (audio children, same gate as single downloads):
            # a storage-owned staged copy is edited and verified; the prepared
            # file itself is never touched. Tags come only from this owned
            # probe: source track first, then the usable persisted child title,
            # then the usable probe title (TITLE tag only; filename and catalog
            # keep their own fallback); playlist order is never a track tag.
            # ``selection`` pins the chosen inode (tagged copy or the rechecked
            # prepared file) until placement and cleanup are finished.
            selection: StagedEditResult | None = None
            tag_fallback = False
            try:
                tags = intended_text_tags(
                    normalize_source_metadata(source),
                    source_title=_tag_source_title(child.title, source.get("title")),
                )
                if (
                    plan.selected.kind == "audio_only"
                    and not remux_fallback
                    and tags is not None
                    and media.suffix.lower() in TAGGABLE_SUFFIXES
                ):
                    await self.check()
                    failures: list[TagError] = []
                    selection = await self.deps.storage.stage_edited_copy(
                        child.id, media, _text_tag_editor(media.suffix, tags, failures),
                    )
                    if failures:
                        # Only a typed tag failure is optional: the intact
                        # prepared output is published instead. Details stay
                        # in the private log.
                        logger.warning(
                            "text tagging failed: job_id=%s child_id=%s kind=%s detail=%s",
                            self.job_id, child.id, failures[0].kind.value,
                            failures[0].detail,
                        )
                        tag_fallback = True
                await self.transition(JobStatus.FINALIZING)
                # Storage owns the shielded thread drain. Signal cancellation
                # through its predicate, rather than racing task.cancel()
                # against that drain. Filename order comes only from the
                # persisted child row (video collections), never from fresh
                # probe metadata.
                self.placing = True
                try:
                    placement = await self.deps.storage.place_child_final(
                        child.id, media if selection is None else selection.path,
                        media_kind="audio" if plan.selected.kind == "audio_only" else "video",
                        title=child.title or "Untitled", cancel_requested=lambda: (
                            self.cancel_event.is_set() or self.stopped.is_set()
                        ), publish_guard=self.cancel_gate,
                        source_order=(child.source_order
                                      if parent.collection_mode == "video" else None),
                        # Same measured short edge as the catalog; unmeasured: no suffix.
                        video_short_edge=(None if dimensions is None
                                          else dimensions.short_edge),
                        # The verified, still pinned inode must be the one published.
                        source_identity=None if selection is None else selection.identity,
                    )
                finally:
                    self.placing = False
            finally:
                if selection is not None:
                    # Closes the pin; only an unpublished own copy is unlinked.
                    selection.discard()
            await self.check()
            # Catalog facts come from this owned probe result only: the real
            # duration when representable, and the existing platform
            # detection semantics (extractor signature, URL host fallback).
            duration_seconds = _safe_duration_seconds(source.get("duration"))
            platform = detect_platform(source, child.source_url).value
            async with self.deps.session_factory() as session:
                try:
                    if not await stage_child_completion(
                        session, self.deps.storage, job_id=self.job_id,
                        owner_id=self.owner_id, child_id=child.id, selected=plan.selected,
                        placement=placement, duration_seconds=duration_seconds,
                        platform=platform, plan=conversion, dimensions=dimensions,
                    ):
                        raise _LostClaim
                    warnings: list[ActivityEvent] = []
                    for enabled, message in (
                        (remux_fallback, _REMUX_FALLBACK_SAVED),
                        (tag_fallback, _TAG_FALLBACK_SAVED),
                    ):
                        if enabled:
                            # Fixed shared text only; the child link lives in details.
                            warnings.append(await activity.add_activity_event(
                                session, job_id=self.job_id, level="warning",
                                event_type="job", message=message,
                                details={"child_id": child.id},
                            ))
                    await session.commit()
                except BaseException:
                    # Persistence contract: a failed COMMIT leaves the SQLite
                    # transaction open; roll back the whole unit explicitly.
                    await session.rollback()
                    raise
                # Committed facts only: the caller publishes this sink even if
                # session close or a later step raises/cancels.
                if self.committed_activities is not None:
                    self.committed_activities.extend(warnings)
            self.deps.storage.remove_staging(child.id)
            return ChildResult.COMPLETED
        except _LostClaim:
            return ChildResult.SKIPPED
        except YtDlpError as exc:
            # Raw extractor detail goes only to the private container log.
            logger.error(
                "yt-dlp collection child failed: job_id=%s child_id=%s kind=%s detail=%s",
                self.job_id,
                self.child_id,
                exc.kind.value,
                exc.detail if exc.detail is not None else "<none>",
            )
            raise JobError(_YT_DLP_KIND_TO_JOB_CODE[exc.kind], detail=exc.detail) from exc
        except FFmpegError as exc:
            raise JobError(_FFMPEG_KIND_TO_JOB_CODE[exc.kind], detail=exc.detail) from exc
        except PlacementCancelled:
            raise asyncio.CancelledError from None
        except JobError:
            raise
        except OSError as exc:
            raise JobError(JobErrorCode.DISK_WRITE_FAILED, detail=repr(exc)) from exc
        except Exception as exc:
            raise JobError(JobErrorCode.INTERNAL_ERROR, detail=repr(exc)) from exc
        finally:
            if self.process is not None and self.processes.get(self.job_id) is self.process:
                self.processes.pop(self.job_id, None)
