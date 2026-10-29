# Downloader — Technical Architecture

**Version:** v0.1  
**Status:** Initial architecture  
**Source of truth for product behaviour:** `PRODUCT_SPEC.md`

---

# 1. Architecture Goals

Downloader should be:

- simple enough to operate as a personal homelab application,
- reliable across container restarts,
- easy to debug,
- safe around filesystem operations,
- responsive on mobile devices,
- capable of showing live download progress,
- easy to extend later with authentication,
- easy to extend later with more advanced workers,
- not unnecessarily dependent on infrastructure such as Redis or Celery.

The v0.1 architecture should optimize for a single private deployment rather than premature horizontal scaling.

---

# 2. High-Level Architecture

Production deployment should initially use a single application container.

```text
Browser
   │
   │ HTTP / SSE
   ▼
┌─────────────────────────────────────────┐
│ Downloader Container                    │
│                                         │
│ React UI                                │
│    │                                    │
│    ▼                                    │
│ FastAPI                                 │
│    │                                    │
│    ├── Analysis Service                 │
│    ├── Job Service                      │
│    ├── Queue Manager                    │
│    ├── Library Service                  │
│    ├── Storage Service                  │
│    └── Activity Service                 │
│                                         │
│    ├── yt-dlp subprocess                │
│    └── FFmpeg subprocess                │
│                                         │
│ SQLite                                  │
└───────┬─────────────┬─────────────┬─────┘
        │             │             │
        ▼             ▼             ▼
    Staging       App Library   Plex Mounts
```

Production should not require:

- Redis
- Celery
- RabbitMQ
- PostgreSQL
- Kubernetes
- multiple backend services

These may only be introduced later if real requirements justify them.

---

# 3. Production Packaging

The preferred production model is one Docker image.

A multi-stage Docker build should:

1. build the React frontend,
2. build/install the Python backend,
3. include `yt-dlp`,
4. include FFmpeg,
5. copy the built frontend into the backend runtime image.

FastAPI should serve:

- `/api/...` API routes,
- `/api/v1/events` SSE stream,
- prepared media downloads,
- the built React frontend.

This provides:

- one origin,
- no production CORS complexity,
- one container to deploy,
- simple Tailscale/reverse-proxy integration.

Development mode may run frontend and backend separately.

---

# 4. Recommended Repository Structure

```text
downloader/
├── PRODUCT_SPEC.md
├── ARCHITECTURE.md
├── AGENTS.md
├── README.md
├── compose.yml
├── Dockerfile
│
├── backend/
│   ├── pyproject.toml
│   ├── migrations/
│   ├── tests/
│   └── app/
│       ├── main.py
│       ├── config.py
│       ├── database.py
│       │
│       ├── api/
│       │   ├── dependencies.py
│       │   └── v1/
│       │       ├── analysis.py
│       │       ├── jobs.py
│       │       ├── library.py
│       │       ├── settings.py
│       │       └── events.py
│       │
│       ├── domain/
│       │   ├── analysis.py
│       │   ├── jobs.py
│       │   ├── library.py
│       │   └── activity.py
│       │
│       ├── services/
│       │   ├── analysis_service.py
│       │   ├── download_service.py
│       │   ├── job_service.py
│       │   ├── queue_manager.py
│       │   ├── library_service.py
│       │   ├── storage_service.py
│       │   ├── metadata_service.py
│       │   └── activity_service.py
│       │
│       ├── integrations/
│       │   ├── ytdlp.py
│       │   └── ffmpeg.py
│       │
│       ├── repositories/
│       │   ├── jobs.py
│       │   ├── analyses.py
│       │   ├── library.py
│       │   ├── activity.py
│       │   └── settings.py
│       │
│       └── utils/
│           ├── filenames.py
│           ├── paths.py
│           └── redaction.py
│
├── frontend/
│   ├── package.json
│   ├── src/
│   │   ├── app/
│   │   ├── api/
│   │   ├── components/
│   │   ├── features/
│   │   │   ├── new-download/
│   │   │   ├── queue/
│   │   │   ├── library/
│   │   │   ├── activity/
│   │   │   └── settings/
│   │   ├── pages/
│   │   └── types/
│   └── tests/
│
└── scripts/
```

The exact naming may change if implementation reveals a good reason, but responsibilities should remain separated.

---

# 5. Backend Stack

Recommended:

- Python
- FastAPI
- Pydantic
- SQLAlchemy 2
- SQLite
- Alembic
- `aiosqlite`
- `yt-dlp`
- FFmpeg

Testing:

- pytest
- pytest-asyncio

Quality:

- Ruff
- static type checking
- backend test suite

---

# 6. Frontend Stack

Recommended:

- React
- TypeScript
- Vite
- React Router
- TanStack Query or equivalent lightweight server-state layer

Testing:

- Vitest
- React Testing Library

The frontend must not duplicate backend business logic for:

- format validation,
- destination validation,
- path generation,
- filename conflict behaviour,
- yt-dlp arguments.

The backend remains authoritative.

---

# 7. API Versioning

All application API routes should be versioned from the beginning.

```text
/api/v1/...
```

This gives future versions room to evolve without breaking clients.

Examples:

```text
POST   /api/v1/analysis
GET    /api/v1/analysis/{analysis_id}

POST   /api/v1/jobs
GET    /api/v1/jobs
GET    /api/v1/jobs/{job_id}
POST   /api/v1/jobs/{job_id}/cancel
POST   /api/v1/jobs/{job_id}/retry
DELETE /api/v1/jobs/{job_id}

GET    /api/v1/library
GET    /api/v1/library/{item_id}
DELETE /api/v1/library/{item_id}
DELETE /api/v1/library/{item_id}/file

GET    /api/v1/settings
PATCH  /api/v1/settings

GET    /api/v1/events
```

Names may change slightly during implementation, but REST semantics should remain clear.

---

# 8. URL Analysis Architecture

Downloads must not start directly from an arbitrary URL submitted by the browser.

The workflow is:

```text
URL
 ↓
POST /analysis
 ↓
Analysis Service
 ↓
yt-dlp probe
 ↓
Normalize result
 ↓
Store short-lived analysis session
 ↓
Return normalized choices to frontend
```

The frontend should receive normalized media information rather than the entire raw yt-dlp response.

---

# 9. Analysis Sessions

A successful analysis should create an `analysis_session`.

Suggested properties:

```text
id
owner_id
source_url
platform_hint
detected_platform
extractor
media_type
title
normalized_result_json
created_at
expires_at
```

The normalized result may contain:

- source metadata,
- available items,
- available audio variants,
- available video variants,
- playlist information,
- estimated sizes,
- metadata confidence information.

Analysis sessions should expire.

A reasonable initial TTL is approximately one hour.

The client should create a download job using:

```text
analysis_id
selected_item_ids
selected_variant_id
destination
conflict_policy
keep_app_copy
```

rather than submitting raw arbitrary yt-dlp arguments.

The fields above illustrate single-item selection. Collection creation additionally carries a collection-level Audio / Video mode and output format/profile preference, defaulting to `Original / Best Available`, rather than assuming one low-level source variant works for every child. Exact payload names and profile representation remain design follow-ups. The backend validates selected items and profiles against the owned, unexpired analysis and enforces the 200-selected-item cap; creation of the parent and all selected children must be atomic.

---

# 10. Format Normalization

yt-dlp exposes many low-level formats.

The user should not be required to understand internal format IDs.

The backend should convert raw formats into application-level variants.

Example:

```text
variant_id: "video-1080-best"
label: "1080p"
kind: "video"
resolution: 1080
container: "mp4"
requires_merge: true
estimated_size: ...
```

Audio example:

```text
variant_id: "audio-original-best"
label: "Original / Best Available"
kind: "audio"
conversion: null
source_codec: "opus"
source_bitrate: ...
```

`variant_id` identifies only a source variant. A conversion target is a separate backend-validated output-profile concept, chosen independently of the source variant (illustrative only; the wire JSON schema, names and enumerations are not frozen):

```text
source selection:  variant_id = "audio-original-best"
output choice:     output profile = one backend-known profile (e.g. MP3)
```

M6 planned (not implemented): required audio profiles are Original / Best Available (default), MP3, WAV, M4A, Opus and FLAC, each with one fixed quality profile and no bitrate slider; required video profiles are Original and MP4 (compatible). Codec/bitrate/quality parameters of each profile are a later technical design item.

M6.4b foundation (implemented): wire field `output_profile` on job create (common to single and collection, outside the single/collection exclusion) and on `JobDto` (default `original`), values `original|mp3|wav|m4a|opus|flac|mp4`; audio set Original+MP3/WAV/M4A/Opus/FLAC, video set Original+MP4, chosen by the single variant kind or the collection mode. Unknown values are a 422 request error. Migration `0007` adds nullable `download_jobs.output_profile` (no default/backfill/CHECK); NULL and Original are the same and read as `original`; other stored values are returned unchanged and unknown ones fail mapping instead of falling back. One parent-level column; children have no output field, and `CollectionProfile` stays the native source-quality preference (`max_height`). Retry/reset/recovery keep the stored value. Temporary fail-closed gate: conversion runtime is not implemented (M6.4c/d), so create rejects every non-Original choice with `invalid_profile` without side effects, and the single runner (after claim) and each collection child (before probe) fail any stored non-Original value with non-retryable `invalid_profile` before analysis/download/storage; existing parent settlement and cancel precedence are unchanged.

The mapping from `variant_id` and output profile to actual yt-dlp/FFmpeg execution parameters must exist only on the backend, as argument arrays.

The browser must never construct shell arguments.

---

# 11. yt-dlp Integration Boundary

For v0.1, yt-dlp should be treated as an external execution boundary.

Preferred execution model:

```text
FastAPI
   ↓
yt-dlp integration adapter
   ↓
async subprocess
   ↓
yt-dlp
```

Use:

```python
asyncio.create_subprocess_exec(...)
```

or an equivalent argument-array based API.

Do not execute constructed shell command strings.

Do not use:

```text
shell=True
```

for user-controlled operations.

Reasons for using a subprocess boundary:

- reliable cancellation,
- process isolation,
- easier resource cleanup,
- clear stdout/stderr capture,
- easier future worker separation,
- failures do not crash the API process,
- yt-dlp can be upgraded independently of application domain logic.

The integration adapter should hide CLI details from the rest of the codebase.

---

# 12. yt-dlp Analysis

Analysis should use yt-dlp in metadata-only mode.

The integration layer should obtain machine-readable JSON and normalize it.

The parser must not depend on human-oriented terminal output for metadata.

If yt-dlp output changes, only the integration adapter should need substantial changes.

---

# 13. yt-dlp Download Progress

Download execution should emit machine-readable progress information.

The yt-dlp adapter should translate progress into internal events such as:

```text
DOWNLOAD_STARTED
DOWNLOAD_PROGRESS
DOWNLOAD_FINISHED
POST_PROCESS_STARTED
POST_PROCESS_FINISHED
```

Example normalized progress:

```json
{
  "percent": 61.4,
  "downloaded_bytes": 123456789,
  "total_bytes": 201234567,
  "speed_bytes_per_second": 7340032,
  "eta_seconds": 18
}
```

The frontend should not parse raw yt-dlp terminal output.

---

# 14. FFmpeg Integration

FFmpeg should also live behind an integration adapter.

Responsibilities may include:

- container merging,
- audio conversion,
- metadata embedding,
- artwork embedding,
- other final media processing.

Where yt-dlp safely handles a required FFmpeg postprocessor itself, that is acceptable.

However, application-level metadata and naming rules must remain controlled by Downloader.

FFmpeg processes should use argument arrays, not shell-concatenated command strings.

M6 decisions (approved; planned, not implemented; PRODUCT_SPEC §8, §14): Original / Best Available keeps source quality. WebM-contained Opus audio is remuxed losslessly into `.opus`; AAC/M4A is kept; MP3/AAC re-encoding is not a default and happens only for an explicitly selected output; past Library items are not reprocessed. Tagging, artwork and remux run on staged files only. If they fail under Original, the safe original is kept and the job completes with an Activity warning; cancellation, security, storage and catalog errors are not swallowed by this fallback, and staging and storage containment boundaries are unchanged.

Explicit conversion (approved; planned, not implemented): conversion runs through this FFmpeg adapter and the existing prepared_media/staging/storage boundaries; there is no separate converter or runner. An explicitly selected output that cannot be produced fails the job/child; no silent publish of the original in a different format. Optional tag/artwork failure never reverts a ready target to the pre-conversion source format. Missing/unsupported encoder error handling is later technical design; no encoder is guaranteed per FFmpeg build. MP4 (compatible) requires real H.264/AAC output (copy/remux of compatible streams, transcode otherwise). Current runtime (`prepared_media.py`, `integrations/ffmpeg.py`) only downloads and merges streams with copy; it does no conversion, tagging, artwork or remux. Historical M5 contracts that rejected conversion/accepted native-only remain a record of M5 and are superseded for new downloads by the M6 plan.

---

# 15. Queue Architecture

Do not use FastAPI `BackgroundTasks` as the durable job queue.

Instead create an application-level `QueueManager`.

```text
SQLite jobs
    ↓
QueueManager
    ↓
Concurrency limiter
    ↓
Job runner
    ↓
yt-dlp / FFmpeg
```

The database is the source of truth for queue state.

An in-memory structure may accelerate runtime operation, but it must not be the only record of jobs.

---

# 16. Queue Runtime Model

The application should run one backend process in v0.1.

A startup/lifespan task starts the QueueManager.

The QueueManager:

1. finds runnable queued jobs,
2. respects configured concurrency,
3. starts job runner tasks,
4. records state changes,
5. emits Activity events,
6. continues checking for queued work.

A simple `asyncio.Semaphore` or equivalent may enforce maximum concurrent jobs.

Default concurrency should be conservative.

Initial recommendation:

```text
2 concurrent jobs
```

The user may change this in Settings.

---

# 17. Why One Backend Process

v0.1 should intentionally run only one application process.

Multiple Uvicorn workers would create competing in-process queue managers.

Therefore production configuration must not blindly start several backend workers.

If future requirements demand multiple backend replicas, queue execution must first move to a dedicated worker architecture.

That is outside v0.1.

---

# 18. Job State Machine

Supported states:

```text
queued
analyzing
downloading
post_processing
finalizing
completed
failed
cancelled
```

Possible transition:

```text
queued
  ↓
analyzing
  ↓
downloading
  ↓
post_processing
  ↓
finalizing
  ↓
completed
```

Failure can occur from any active state:

```text
active state
    ↓
failed
```

Cancellation:

```text
queued/active
    ↓
cancelled
```

The backend must validate state transitions.

The frontend must not be able to arbitrarily set job state.

---

# 19. Playlist Job Model

A playlist or album should be represented as one parent job with child items.

```text
Download Job
│
├── Item 01
├── Item 02
├── Item 03
└── Item 04
```

The parent tracks:

- total selected items,
- completed items,
- failed items,
- aggregate progress.

The parent also owns the collection-level Audio / Video mode and output format/profile preference. There is one preference for the job, not per-item profiles in v0.1. The backend resolves it against each child's actual source through the yt-dlp/FFmpeg integration boundaries. `Original / Best Available` chooses the most suitable available/native result, allowing heterogeneous final containers/codecs. Explicit conversion affects only this job's selected children, never existing Library or historical files. Video ceilings such as `Up to 1080p` do not upscale lower-quality sources, and conversion is not quality enhancement. M6 plan (approved; not implemented) supersedes the earlier optional-profile wording: MP3, WAV, M4A, Opus and FLAC audio and MP4 (compatible) video are required new-download outputs (§10, §14, §59 M6); the runtime above still accepts only the native profile.

Collection analysis exposes selectable items and their source ordering. Enforce a hard maximum of 200 selected items per job, not 200 total source items. `Select All` is limited to currently available, analyzed, selectable items within the cap; unseen or over-cap items must not be silently included. Large-source UI must explain this v0.1 limit. Entire channel/profile bulk operations are out of scope.

Each child item tracks its own:

- source ID,
- title,
- track/index,
- state,
- progress,
- output file.

A terminal mixed result with some completed children and at least one failed child sets the parent to `failed`, retaining accurate completed/failed/total counters. Do not add a partial-success state or invalidate completed children, their finalized files, or Library records. The stable parent error code `partial_failure` summarizes that mixed result only: it is not a job state and must not replace child-specific error codes/messages. Retry must exclude completed children.

Parent retry reruns only failed, cancelled, or incomplete child work; completed children are never downloaded or reprocessed again. v0.1 provides parent-level controls only, not per-child Cancel / Retry UI.

M5 current implementation status: production collection dispatch, sequential child execution,
atomic child/catalog completion, durable Cancel (migration `0006`), Retry, startup recovery and
orderly shutdown settlement are implemented. Single behaviour is preserved. M5 passed independent
milestone review and fresh full gates and is accepted; historical design assignments below are not
instructions to restart completed work.

The original technical follow-ups included controlled identity for missing/duplicate source IDs and stale selections, analysis retrieval, aggregate percent calculation, and child scheduling/concurrency-slot semantics. M5-1 below resolves snapshot identity and bounds retrieval; the M5-4B design note below selects aggregate-percent and slot mechanics, not additional product policy. All-failed error aggregation and cancellation-versus-failure precedence are decided product policy (PRODUCT_SPEC section 17): an all-failed parent is `failed` with stable `all_children_failed` semantics and Retry requires a retry-eligible failed/cancelled/incomplete child, and cancellation-versus-failure precedence follows durable persisted state (Cancel persisted before terminalization wins; a committed failure terminal cannot be superseded). Collection activation followed implementation of collection controls, retry, recovery and drain; the single runner's precedence is unchanged.

M5-1 analysis technical choice: retain the existing bounded first-25 flat-entry retrieval, separately from the 200-selected-item job cap; no pagination, entire-source retrieval or child format probing is added. Normalized `items` expose snapshot-scoped backend IDs derived from analysis ID and source slot order, independently of nullable/duplicate source IDs or playlist indices. `source_order` preserves slots (including unavailable/malformed entries); `position` remains source metadata. Only entries with an existing-boundary-validated HTTP(S) URL and no explicit unavailable/auth-only status are selectable; URLs are never synthesized from IDs or extractor arguments. `preview_items` remains backward compatible. Selection scope/count/truncation and a v0.1 cap message describe only this window, not all-source selection; without a reliable source count, `total_items` is a lower bound and `total_items_known` is false. Old stored previews default to no selectable items and require reanalysis. IDs and ordering round-trip through the owned persisted snapshot; future M5-3 validates references against that same owned, unexpired analysis, never a fresh extraction. This minimal retrieval budget avoids unbounded source work while making the current available selection truthful; broader retrieval remains a technical follow-up.

M5-3 create/read technical contract: existing `analysis_id + variant_id` single requests remain valid. Collection requests instead carry `item_ids`, required `collection_mode` (`audio` / `video`) and optional `collection_profile`: `{kind: "original_best_available", max_height?: positive integer}`. A height ceiling is video-only and a source-aware preference, never an upscaling/quality guarantee. Unknown fields, conversion names, raw formats/URLs/CLI arguments and mixed single/collection requests are rejected. Omitted/null/native-default profile persists as SQL NULL; a video ceiling persists as preference JSON. This deliberately narrow contract establishes no conversion matrix; child probing/resolution/execution remains M5-4. Owned unexpired persisted selectable IDs are validated without extraction, with distinct nonempty selections capped at 200 and raw source order retained. Parent, children and initial activity commit together, before live publication/queue notification. Collection POST/GET/list/control responses include owner-scoped children; shared SSE mapping carries parent preferences/counters but leaves unrequested children null. Single DTO counters are null, not the unmaintained migration defaults. Child paths follow the existing read-only parent path boundary; no file operations are added.

**Historical M5-3 execution boundary (superseded by production queue binding).** `claimable_queued_jobs` filters to `job_type=single` before applying its concurrency limit. Collections remain queued and cannot reach the single runner or starve single queue draining. This is an internal transition until M5-4 supplies collection execution, not supported collection downloading or a new user-visible state. Collection cancellation/retry and child lifecycle hardening remain M5-5; this task does not add child scheduling/CAS or change single cancel/retry semantics.

---

Historical M5-4A native-resolution foundation (dispatch subsequently implemented): `YtDlpAnalyzer.probe_child` revalidates the persisted backend child URL through the existing HTTP(S) boundary, uses metadata-only `--skip-download --no-playlist` without flat retrieval, and rejects collection-shaped output without recursively probing entries. Timeout and task cancellation kill/reap the subprocess. `normalize_source_formats` retains every usable stream without the UI's resolution/identity collapse; existing public normalization and flat-25 analysis are unchanged. The pure backend-only resolver returns a selected native format plus an optional audio-only companion. Video candidates must be muxed or have an actual companion; rank actual height, width, then FPS, using yt-dlp's worst-to-best source format preference order as the remaining tie-break. Audio follows that extractor preference order rather than comparing bitrate across codecs; numeric bitrate/size is not a fidelity claim. Unknown dimensions rank below known dimensions; with an explicit height ceiling, unknown height is excluded because compliance cannot be established (source `HEIGHTp` / `WIDTHxHEIGHT` labels may establish height). No candidate above the ceiling and no upscale, extraction, conversion, codec matrix, naming, or output-file changes are introduced. Missing/unusable formats and collection-shaped results use existing malformed-output errors; unavailable modes/ceilings use invalid-variant, unsupported preferences use invalid-profile. Scheduling, aggregate policy and collection execution remain a separate bounded task.

The child CLI also fixes `--playlist-items 1` as a defensive retrieval bound: yt-dlp's `--no-playlist` alone does not suppress a playlist-only URL. Any returned collection is still rejected, never walked by application code. Backend source normalization treats boolean, non-finite, non-positive and fractional dimensions as unknown rather than truncating them into ceiling compliance.

## M5-4B execution contract — implemented; original design record

Status: the execution contracts below are implemented by the accepted M5 backend slices and production queue binding. The original assignment notes are retained as historical design evidence, not current next-task instructions. M5 has since been accepted.

### Scheduling and attempt ownership

- Keep the existing configured global job limit: one single runner or one collection parent runner occupies one QueueManager `_tasks` slot. A parent retains that slot through child probing, downloading, merging, placement, catalog commits and helper/subprocess drain. Children do not take additional queue slots and are not independently dispatched jobs. Run at most one child pipeline per parent, and its video/audio downloads and merge sequentially. Thus with limit K, active parent/single executions and active downloader processes are each at most K, never K times a per-collection fan-out. Retaining the slot for the whole attempt matches the existing single queue guarantee; it deliberately offers no intra-collection parallelism or new fairness policy.
- Visit eligible persisted children in ascending raw `source_order`, preserving gaps and duplicate/missing source IDs. Do not renumber from selection request order or sort by `playlist_index`, title or source ID. Finish/drain one child's work before starting the next. Completed children are excluded before probing as well as before downloading/finalization. Parent claims and child transitions require database compare-and-set; selecting a queued row is not itself a claim.
- The parent owns cancellation signal, placement publish gate and attempt lifetime. Because children are sequential, the existing parent-keyed process registry can represent the current download/merge, but the metadata probe is currently an owned adapter task, not a registered downloader process. Collection execution must own and cancel/await that probe task, including its shielded kill/reap cleanup, on parent Cancel/shutdown. Polling the cancel event only after `probe_child` returns is insufficient. Check cancellation before every child/step and handle cancellation arriving between check and process/task registration. Close callbacks and drain helpers before moving to the next child or releasing/resetting the parent attempt; callbacks bind the child identity and cannot write to a later child/attempt. Single cancellation/failure precedence remains unchanged and is not a collection product decision.

### Truthful aggregate progress

Use finalized-item completion percentage, not byte-weighted transfer percentage or percentage of attempts settled: for N persisted selected children and C children with committed `completed` state, parent `progress_percent = 100 * C / N`. Collection creation requires N > 0; an empty/corrupt persisted selection must not yield division by zero or invented 100% success. Each completed child contributes one, every queued/active/failed/cancelled child contributes zero. For example, two completed plus one failed out of three is 2/3 completion, not 100%; one completed plus an active child with unknown size out of two is 50%. A download callback reporting 100% does not complete the child while merge, placement or catalog work remains. All completed children yield 100%; terminal failure/cancellation alone never does.

This conservative, stepwise formula avoids fabricated byte totals, duration/size weighting across heterogeneous sources, arbitrary probe/merge weights and treating failures as completed output. PRODUCT_SPEC does not require smooth fractional transfer progress; its Queue percentage is an example, and aggregate calculation is explicitly a technical follow-up. Child transfer observations remain inspectable in existing child progress fields, but are not promoted to parent completion facts. If a later requirement defines parent percentage as bytes transferred, elapsed-time estimate or settled attempts, stop and reconcile that product change rather than silently reinterpret this contract. Do not change single percentage mechanics.

Derive `total_items` from the persisted selected set, `completed_items` from completed child states and `failed_items` from failed child states, not callback counts or source collection totals. Cancelled is not failed. Retry keeps N and C, resets only eligible noncompleted child state/progress/errors, and recomputes F; do not apply the single retry's zero-percent reset to preserved collection completion. Update child state and parent counters/percentage in the same caller-owned transaction for each lifecycle change. Late progress cannot modify terminal children or their aggregate. Parent byte totals and collection ETA stay unknown (existing nullable fields) unless genuinely established for the whole selection; do not copy a current child's byte total/ETA into them. Speed, if reported, is only the currently measured transfer rate, and clears outside that transfer.

### Native child execution and atomic catalog completion

- Probe only the persisted child URL through `probe_child`, then resolve the parent's supported native preference with `resolve_native_child`; the parent's empty legacy `variant_id` is never a source format. Keep a resolved plan internal to that attempt, reprobe noncompleted children on retry, and do not mutate persisted selection identity/order from fresh metadata. Use the real selected/companion formats through existing downloader/FFmpeg adapters; no source-total extraction, conversion matrix, M6 naming or existing Library reprocessing. The implemented child execution boundary uses `--no-playlist` and `--playlist-items 1` to prevent playlist-only expansion and validates a single expected output. Preserve the existing single CLI/behaviour rather than assume the probe makes a later source response safe.
- Use backend-generated `JobItem.id` as the child's storage staging key, never a source ID, snapshot `item_id` or client path. This fits the existing validated storage key boundary and isolates retries/cleanup from sibling/completed outputs. All path construction, placement, identity verification and cleanup stay behind StorageService; keep existing skip-existing placement and shared-reference deletion rules.
- Minimal catalog identity choice: for collection children, set `LibraryItem.id = JobItem.id`, with `LibraryItem.job_id = parent.id` and inherited owner. This gives a stable per-child primary-key identity and parent Activity/job linkage without overloading `job_id` with a nonexistent DownloadJob child or adding a schema/API field. Single Library UUID generation remains unchanged. Nullable/duplicate source IDs and same physical paths are not dedup keys: distinct selected children may legitimately create distinct logical rows referring to the same skip-existing file. Populate child source/title and actual resolved/placed media facts; preserve raw source order in JobItem, and use track/disc metadata only when actually known, not an invented contiguous track number. Existing Library newest-first pagination is unchanged.
- Place the file through storage first, then in one short caller-owned database transaction acquire the write lock via conditional child completion (also validating parent/attempt eligibility), stage its LibraryItem/MediaFile with `stage_media_catalog_reference`, re-verify `placement.identity` under that held lock, and recompute parent counters/percentage. Commit all or none; publish snapshots only after commit. No commit-owning `update_job_if_status` helper may split this completion transaction. A losing CAS creates no catalog row; insert/verification/counter failure rolls back child completion and aggregate together. A placed file can survive a database failure, but must never be reported as completed without committed catalog completion (§51).
- Idempotence is based on immutable completed child state plus its stable catalog primary key, not a pre-insert existence check. Repeated/stale completion must lose CAS, and an unexpected identity collision must fail/roll back, not silently skip/upsert an unrelated row. Retry/restart never reset or recreate completed children, even if the user later removes their Library record; Library removal is not permission to download again. Await old helpers before requeue, so status-only CAS cannot suffer an old-attempt callback after reset. Restart recovery must update interrupted noncompleted children and parent aggregates together while leaving completed files/catalog untouched; today's recovery updates DownloadJob only.

### Product decisions and activation gate

The two former product questions are decided and recorded as product source in PRODUCT_SPEC section 17; this note only records them:

1. All-failed collection: the parent is `failed` with stable `all_children_failed` error semantics, stable across homogeneous and heterogeneous child failure reasons. Child errors/counters remain intact; `partial_failure` is not substituted and the first/last child error is not selected, and no new public error contract is invented. Parent Retry is enabled only when at least one retry-eligible failed/cancelled/incomplete child exists; the locked child retry-eligibility classification is recorded below; exact error message wording, migrations and new public API surface are not defined by it.
2. Cancel-versus-failure precedence is decided by durable persisted state: a Cancel request persisted before the parent's terminalization transaction yields a `cancelled` parent; a failure terminal committed first leaves the parent `failed`, and a later Cancel cannot supersede it. In-memory event timing is never the decision source; the single runner's precedence is unchanged and its cancel-event-first logic is not copied. Completed children and their outputs remain immutable. This decision is documented at the transaction observation/ordering level only and does not choose all-completed/success-versus-Cancel semantics, shutdown policy or any new durability/data-schema choice.

**Historical activation prerequisites (now implemented).** These decisions do not block the guard-preserving internal runner foundation below, and they are no longer product-decision blockers for these two points. These activation prerequisites have since been implemented and received bounded review. Activation also requires working collection Cancel, retry of failed/cancelled/incomplete work, completed-child preservation, probe/subprocess cleanup, attempt drain and restart safety; the existing parent-only single cancel/retry/recovery methods are insufficient. The `partial_failure` and `all_children_failed` codes and terminal semantics are now implemented. Do not remove the single-only claim filter merely because a happy-path child executor exists.

### Original foundation assignment and targeted acceptance (historical)

Historical foundation assignment (completed): **guard-preserving prepared-media runner foundation**. Extract the single runner's existing sequential selected-format/optional-companion download and merge operation into an internal seam that accepts a backend-resolved plan, source URL, storage staging key and attempt-owned cancellation/process/progress hooks. Keep parent/single state transitions, placement/catalog completion, Activity and existing single progress mapping under the current single runner. Exercise native child plans through controlled tests only; no production child dispatch, parent terminal reducer, API change or collection lifecycle activation. This is the shared execution boundary needed by the later child pipeline, not a collection happy-path feature exposed without controls.

Acceptance/tests for that assignment: existing single muxed/audio/video-plus-companion output, merge-container choice, progress, errors and catalog transaction behaviour unchanged; controlled heterogeneous native plans/ceiling-selected formats use their actual source selectors, one pipeline at a time, isolated storage staging keys, cancellation checks and process registration hooks. Verify no next stream/merge begins after cancellation and no callback/helper outlives its attempt. Retain the actual collection queue-guard regression and verify an earlier queued collection cannot consume the single LIMIT. Target `tests/test_jobs.py`, `tests/test_job_retry.py`, `tests/test_collection_jobs.py`, `tests/test_library_catalog.py` and the literal new foundation test file if introduced; resolver/adapter files only if changed. Run only relevant configured checks, not milestone/full gates.

Historical subsequent slices (now implemented): child lifecycle + atomic completion/aggregate persistence behind the same guard, then integrated collection controls/restart/terminal reducer and dispatch activation once the decided product contracts above and the required safety controls/tests are implemented. Activation acceptance must include mixed singles/collections at configured limits 1 and 2 with downloader counts bounded, raw source ordering, unknown totals/failures and real counters, duplicate-source/same-path identity, catalog insert/verification rollback and Delete File races, repeated completion, cancel during probe/download/merge/placement, retry waiting for old callbacks and skipping completed children, and restart preservation. A happy-path-only activation is not an acceptable intermediate slice.

Historical evidence inspected when this design was adopted: `backend/app/services/queue_manager.py` (`_claim_and_run`, `request_cancel`, `wait_for_attempt`, `reset_attempt`); `services/job_runner.py` (`_execute`, `_progress_forwarder`, `_complete`, `drain_helpers`); `services/job_service.py` (`create_job`, `retry_job`, `_cancel_queued`); `repositories/jobs.py` (single-only claim, caller-owned stage CAS, single retry/recovery); `models/jobs.py` (child identity/order), `models/library.py` (catalog primary key and parent job linkage); `services/library_service.py:stage_media_catalog_reference`, `services/storage_service.py` (staging validation, placement gate and verification), `repositories/library.py` (staged inserts); and `integrations/ytdlp.py:probe_child`, `services/collection_profile_resolver.py:resolve_native_child` (all paths relative to `backend/app/` after the first). Existing tests reviewed: `backend/tests/test_jobs.py:test_occupied_slots_bound_execution_and_release_drains_queue`, `test_job_retry.py:test_retry_waits_old_unwind_and_progress_and_resets_cancel_runtime`, `test_collection_jobs.py:test_persisted_create_read_list_and_actual_queue_guard`, `test_collection_repository.py:test_roundtrip_owner_isolation_order_and_caller_activity`, and `test_library_catalog.py` catalog-insert rollback, lost-completion CAS and waiting-writer/Delete File regressions. These tests establish existing guarantees, not collection execution coverage.

### M5-PARENT-LIFECYCLE-DESIGN — implemented durability boundary; historical design record

This supersedes the historical next-slice label above, not its execution contracts. Historical baseline at design adoption (superseded by migration `0006` and lifecycle/queue integration): the prepared-media foundation, internal child runner and caller-owned child/catalog persistence exist; collection dispatch remains single-only guarded. `DownloadJob`, `JobItem` and migration `0005` contain no durable Cancel request or attempt generation. QueueManager events/gates are volatile; `JobService._cancel_job` signals before writing, and single `JobExecution._fail` chooses by that event. Neither is a collection durability mechanism. Existing collection CAS checks owner/kind/active parent and child status, but not a Cancel request; no parent terminal reducer, collection reset or child recovery is implemented.

**Minimal representation (implemented by migration `0006`).** Migration `0006` adds one internal nullable `DownloadJob.collection_cancel_requested_at` DateTime column, initially NULL for all existing rows, in revision `0006` after `0005` (leave `0005` unchanged). Non-NULL means a committed request for this collection attempt; its timestamp is diagnostic, never compared with failure timestamps. A boolean would suffice but lose request time; an Activity-only marker would require querying history for every eligibility check and coupling control state to log retention. Reusing `status=cancelled` would conflate request acceptance with settlement before owned work drains. An attempt-ID/request table is unnecessary under the existing single-process, mandatory-drain-before-reset contract; this field is not a cross-process fencing token. No public field/state/endpoint is introduced and singles neither read nor set it.

**Database ordering, not read/event ordering.** Add field-scoped, caller-owned staging helpers using SQLAlchemy `update(...).where(...)` and rowcount, as current collection CAS does. Use a fresh short transaction with no preceding eligibility SELECT/ORM autoflush; SQLite WAL still serializes writers (`app/database.py` configures WAL and busy timeout). Do not use `SELECT FOR UPDATE`, an asyncio lock alone, or commit-owning `update_job_if_status` to establish this boundary.

- Cancel acceptance's first statement conditionally updates the marker only for matching parent ID, owner, `job_type=collection`, and status in queued/active, with a NULL marker. Commit request plus any request Activity together, then signal the owned attempt/wake/publish. A duplicate request may observe the existing marker under the same write boundary and be idempotent while nonterminal; it must not create another request or reset the timestamp. A zero-row result must revalidate persisted owner/kind/status, never signal from a stale snapshot. Any terminal parent rejects late Cancel; specifically failure committed first stays failed. Request rollback/commit failure produces no runtime signal or live event.
- Failure-race staging starts by a conditional parent write (for example `updated_at` only) with matching ID/owner/collection/nonterminal status, acquiring the same SQLite writer boundary **before** reading marker or children. Under that held lock, refresh persisted data (`populate_existing`/column queries, not an old identity-map record). **Implemented eligibility:** read the full persisted selected-child set and derive N (all rows), C (`completed`) and F (`failed`) from those actual rows, never cached parent counters; eligible iff N > 0, every child status is in {`completed`, `failed`}, F >= 1 and C + F = N. Any queued, analyzing, downloading, post_processing, finalizing, cancelled, incomplete or unknown status excludes settlement even with a non-NULL marker; empty/missing/corrupt selections cannot be made eligible by inventing rows/counts or 100% progress. All-completed (F = 0) is excluded with or without a marker. Only for this eligible set, stage parent terminal state, error, actual counters, C/N percentage and Activity atomically: non-NULL marker selects `cancelled` by the locked durable ordering, without changing child statuses/errors or fabricating cancelled-child counts; NULL marker selects `failed/all_children_failed` for C = 0, F = N (homogeneous or heterogeneous reasons), or `failed/partial_failure` for C > 0, F > 0. An ineligible set or lost initial/final CAS returns a no-settlement outcome requiring caller rollback of the entire fresh transaction, including the lock-touch; never commit that touch or invent a terminal/error/Activity. The previously committed marker and all current persisted state remain unchanged. Write/commit failure likewise requires whole-transaction rollback; no settlement publication before successful commit.
- The final parent CAS retains owner/kind/nonterminal conditions and the marker branch condition (NULL for failure, non-NULL for cancellation). Thus a reducer that previously read NULL cannot overwrite a subsequently accepted Cancel. If Cancel commits first, the terminal writer sees it; if the failure writer holds the lock first and commits failed, the waiting Cancel update matches no nonterminal row. A rolled-back request is not accepted, a rolled-back failure is not a terminal, and lock/busy/snapshot errors require rollback and fresh re-evaluation, never fallback to stale values. Transaction entry time and wall-clock/event order are not precedence. No subprocess/probe/placement/drain wait occurs while holding the writer lock.

**Child/catalog and attempt lifetime.** Future collection claim and child forward/progress/completion CAS must also require a NULL request marker in their persisted parent predicate. Actual child failure recording can retain the nonterminal-parent condition without hiding a real error merely because Cancel was accepted. Completion and Cancel share SQLite's writer serialization: a committed child/catalog completion before the request remains immutable; completion waiting behind an accepted request loses eligibility and inserts no catalog row. Keep the existing storage publish gate, owned helper/thread/subprocess drain, completion CAS -> catalog insert -> identity reverify -> aggregate transaction. File publication can precede the durable request while catalog completion loses; that is a placed-but-uncompleted file (§51), not permission to fabricate completed or delete a completed sibling. Runtime cancellation only accelerates stopping; durable predicates close the commit-to-signal gap. Retain one parent slot through drain, sequential raw source order, stable child catalog identity, unchanged N/C and truthful recomputed F/C-over-N percentage.

The marker survives crashes and terminal settlement; events are reconstructible from it. Before scheduling/recovery in later lifecycle work, observe a queued/active request and prevent new children from starting; interrupted noncompleted child changes and parent aggregates must be atomic, preserving completed files/catalog. In the defined Cancel-versus-failure recovery branch, a persisted request must not be overwritten by today's blanket interrupted-parent failure update. Clear the marker only in a successful collection retry/reset transaction that revalidates eligibility and requeues eligible noncompleted children, never on claim, event clearing, failed reset, or startup. Hold the lifecycle lock through old attempt/callback/probe/storage/helper drain, transaction outcome and post-commit runtime reset; repeated request cancellation must not release it early. No old callback may run after reset: status-only CAS plus this marker cannot fence an undrained old attempt. A crash after reset commit leaves a queued fresh attempt with NULL marker; a crash before that commit retains the old request/state. These integration obligations are implemented by the accepted collection lifecycle and recovery/shutdown slices.

**Historical Retry integration planning (implemented where specified by the locked contracts below).** Retry eligibility and explicit policy limits. Reuse `domain/jobs.py:job_error_code_retryable` for known failed-child codes where applicable; do not broaden its list or interpret every failed child as retryable. Collection parent eligibility is an existence check over retry-eligible failed/cancelled/incomplete children, never the aggregate parent code. At design adoption, `stage_retry_job`, `JobService.retry_job` and `job_mapping` instead classify the parent error and reset single progress; they cannot be reused unchanged. The helper returns false for cancelled/unknown codes, and no persisted per-error `JobError.retryable` override exists. The locked classification below makes all cancelled children eligible regardless of reason/code, defines drained queued/active children under terminal failed/cancelled parents as incomplete, rejects missing/unknown failed codes, and disallows transient retryable overrides of persisted code classification; implementation remains separately scoped. Adding `partial_failure`/`all_children_failed` enum/message support belongs to the future internal failure-race slice; those summary codes must not conceal eligible child errors. All-failed state/code and durable Cancel-versus-failure are already locked, not questions to reopen. The proposed child-state subset is a guard-preserving internal primitive boundary, not a product restriction or general parent reducer: Cancel acceptance still persists requests for queued/active parents without this child-set predicate. The queued/active-child Cancel settlement and all-completed precedence product decisions are recorded below; their lifecycle implementation, broader reduction, unsettled cancelled/incomplete combinations, retry implementation and shutdown contracts remain separately scoped. Persisted eligibility proves no active child row, not full runtime quiescence: before invoking terminal staging, the caller must await all attempt-owned work/callbacks/probes/subprocesses/storage helpers **outside** the writer lock; this helper neither drains work nor replaces that obligation.

**Locked collection Retry eligibility (implemented contract).** User-locked decisions implemented by the collection Retry lifecycle: all cancelled children are retry-eligible regardless of reason or persisted code; Parent Retry is permitted only on a terminal failed/cancelled collection parent, while completed, queued/active and unknown/corrupt parents reject it; after the full old-attempt drain, old-attempt queued/analyzing/downloading/post_processing/finalizing children count as incomplete and are retry-eligible, with the queued state prepared inside the successful atomic retry/reset and not persisted as a new status. Code authority is unchanged: known persisted failed codes stay governed by the existing `job_error_code_retryable` list, which is not broadened; missing or unknown persisted codes are ineligible, and transient `JobError.retryable` never overrides the persisted code classification, so no new persisted override is introduced. Parent eligibility is the existence of at least one retry-eligible child, never the aggregate parent error code. No-go constraints stay literal: completed children and their outputs are immutable and never rerun, even after Library deletion; the retry settlement preserves the actual N/C counters with no new cancelled counter; and the marker constraints above — the drain-held lifecycle lock through transaction outcome and post-commit runtime reset, the marker cleared only in a successful collection retry/reset transaction and never on claim, event clearing, failed reset, or startup, and status-only CAS plus this marker cannot fence an undrained old attempt — are referenced as-is, with no new lock or CAS design. The known failed-code list is not broadened, no new child statuses, override API, recovery or shutdown policy is introduced, and Retry activation remains outside this classification.

**Locked Parent Cancel lifecycle decisions (implemented contract).** User-locked product decisions implemented by the collection lifecycle: after a durable Parent Cancel acceptance and the full attempt-owned drain, queued or active children (the active states analyzing, downloading, post_processing, finalizing) become `cancelled` in one atomic settlement that writes child terminal states and the parent terminal state, actual N/C/F counters and Activity under the existing drain/write/commit boundary; already-failed child states and reasons stay real, completed children, outputs and catalog remain immutable, and no new cancelled counter is introduced. The all-completed precedence is expressly confined to all selected children completed: when every selected child's completion is durably committed (a nonempty all-completed selected set) and the parent success terminal is not yet committed, a committing Cancel marker settles the parent as `completed`, not `cancelled` (children win); a success terminal committed first rejects a late Cancel. This all-completed (F = 0) decision is not an extension of the narrow F >= 1 failure-race staging guard, which stays retained with its locked precedence — a Cancel request persisted first yields `cancelled`, a failure terminal committed first leaves the parent `failed` — and the NULL-marker child claim/forward/progress/completion CAS predicates stay unchanged, with no post-marker completion or catalog insert and no marker clear on success. No new transaction approach or migration design is introduced; lifecycle implementation, broader reduction, unsettled cancelled/incomplete combinations, retry implementation and shutdown contracts remain separately scoped.

**Locked collection startup recovery precedence (implemented contract).** User-locked product decision recorded with PRODUCT §17 and the §21 collection exception below: at startup recovery, a previously active collection parent whose full nonempty actual selected child set is already committed with statuses only completed or failed settles from actual child facts before interruption normalization — all completed (C = N, F = 0) → `completed`; F = N → `failed` with stable `all_children_failed`; C > 0 with F > 0 and C + F = N → `failed` with `partial_failure`. The settlement writes atomic child/parent actual N/C/F counts and Activity with no new CAS algorithm or migration; cached parent counters and event timing are not decision inputs, existing completed catalog/outputs and failed reasons stay immutable, recovery is applied before new child claims, existing terminal parents are unchanged, and a queued parent with a NULL marker stays queued. A persisted Cancel marker keeps the locked Parent Cancel matrix above authoritative with the marker never cleared at startup; without a marker the §21 interruption normalization stands — the interrupted active child becomes `failed`/`interrupted_by_restart`, queued children are unchanged, existing failed/completed children are preserved, and the parent becomes `failed`/`interrupted_by_restart` with real atomic N/C/F. The locked recovery branch is implemented; no cancelled/unknown/corrupt-set, auto-resume/Retry or shutdown policy is introduced.

**Locked collection orderly shutdown settlement (implemented contract).** User-locked product decision recorded now with
PRODUCT §17 and the §21 note below: `queue_manager.stop()` stops new claims, awaits the full drain of
collection attempts and ongoing API Cancel/Retry operations (including probes, downloader/merge
processes, storage-gate helpers, callbacks, owned transactions and session close), commits collection
durable settlement after that drain and before returning, and only then allows `engine.dispose()` to close
the database. The settlement reuses the locked startup recovery matrix above unchanged: already-settled
completed/failed facts take their normal precedence before interruption normalization (all completed
(C = N, F = 0) → `completed`; F = N → `failed`/`all_children_failed`; mixed (C > 0, F > 0) →
`failed`/`partial_failure`), a pending actual active child settles `failed`/`interrupted_by_restart` with the
parent `failed`/`interrupted_by_restart` and real atomic N/C/F, queued siblings are unchanged, and a
committed Cancel marker keeps the locked Cancel matrix authoritative with no marker clear. No callback
or runtime event timing is a decision input, and no synthetic User Cancel marker is written. Existing durably
terminal parents stay unchanged; completed outputs, catalog entries and persisted failed reasons are
preserved; unclaimed queued jobs stay queued, and an already committed Cancel obeys the existing Cancel
contract with no invented ignore-marker behaviour. The full owned drain is a mandatory technical constraint
recorded per the existing pattern: the database stays open until settlement operations finish, and the
SQL writer and lifecycle locks are held outside process/helper waits to avoid a stop-phase deadlock;
this records no new coordinator API or implementation design. Existing single-job shutdown remains unchanged,
and this is a collection-only decision. Not chosen and unsupported: unknown/corrupt/empty sets,
cancelled children under a NULL marker, new status/API/migration, auto-resume/Retry policy, and any
new durability guarantee on SIGKILL or unbounded force-kill. A shutdown that cannot finish or commit
claims nothing and falls back to the existing full startup recovery at the next start, with no new
timeout policy. Collection orderly shutdown settlement and production dispatch are implemented and accepted with M5.

The following assignment and its original no-go limits describe the completed persistence-only slice, not the current production activation state.

**Historical internal persistence assignment (completed; not the next task).** Implement only the new nullable marker migration/model parity, collection request staging and the narrow internal failure-race staging primitive with the proposed N > 0, statuses only {completed, failed}, F >= 1, C + F = N predicate above (including mandatory whole-transaction no-settlement rollback), plus marker-aware collection claim and all child forward/progress/completion CAS eligibility, preserving actual child failure recording as the nonterminal-parent exception. Include aggregate failure enum support needed by this primitive, not a general parent reducer or Retry/API activation. Narrow intended files: `backend/migrations/versions/0006_collection_cancel_request.py` (proposed name/revision), `backend/app/models/jobs.py`, `backend/app/domain/jobs.py`, `backend/app/repositories/jobs.py`, `backend/app/services/collection_persistence.py`; tests `backend/tests/test_collection_migration.py`, `backend/tests/test_collection_persistence.py`, `backend/tests/test_collection_repository.py`. Caller-controlled internal tests establish commit boundaries; production `job_service`, QueueManager, runner, mapping and endpoints remain unchanged. Lifecycle signal/publication integration, runner settlement/drain, retry classification/reset, recovery and activation follow only in separately assigned bounded work, not this slice.

Historical acceptance for that completed assignment: isolated temporary SQLite migration/model parity and unchanged legacy singles/children/catalog; independent-session controlled writer-order tests for Cancel-first, failure-first rejection, stale NULL read then Cancel commit then failure, request/failure rollback and commit failure, duplicate/foreign-owner/wrong-kind/terminal conditions. R1 predicate tests must reject failed+queued; failed+each active status (analyzing/downloading/post_processing/finalizing); completed+failed+each of cancelled/incomplete/unknown; and empty/all-completed sets, each with and without a marker. Verify requests can still commit for queued/active parents with ineligible sets; subsequent no-settlement rollback preserves the committed marker, parent status/error/counters/progress/timestamps, child rows, catalog and Activity, including rollback of the lock-touch. Reject missing/corrupt selections without synthesized counts/progress; use deliberately stale cached parent counters to prove eligibility and settled C/N/F come from actual child rows. Homogeneous/heterogeneous all-failed and eligible mixed completed+failed sets must produce the stable failure codes with NULL marker, or cancelled with non-NULL marker, preserving actual child errors/C/N/F and completed outputs without fabricating cancelled children; lost final CAS/write/commit failure leaves no partial terminal/error/Activity. Child completion-first retained versus request-first losing CAS with no catalog insert, existing identity/insert/counter/verification rollback and Delete File race preserved. Implemented marker-gated eligibility tests: after a committed Cancel request, the collection claim (queued→analyzing) is rejected; the subsequent native forward transitions analyzing→downloading, downloading→post_processing/finalizing and post_processing→finalizing all reject; downloading progress and finalizing completion lose with no catalog insert; every losing gated CAS leaves child status/progress/errors/timestamps and catalog/parent aggregates unchanged; NULL-marker normal eligible claim/forward/progress paths still pass and the existing completion-first branch is retained; and actual child failure recording under an owned nonterminal parent remains possible after the marker, preserving the reason and atomically recomputed aggregates, while unsafe revival of completed/terminal or wrong-owner children stays rejected. No event/wake before durable commit is a later lifecycle acceptance gate, not a claim of the staging-only slice. Run only configured scoped Ruff/type checks and the literal relevant test files, including `backend/tests/test_collection_jobs.py` for the real single-only guard and `backend/tests/test_job_retry.py` for unchanged single retry/drain. No full gates or dispatch-filter removal.

Evidence for this subsection: `backend/app/database.py:35–49`; `domain/jobs.py:59–69,129–158`; `models/jobs.py`, migration `0005_collection_jobs.py`; `repositories/jobs.py:stage_collection_claim`, `_stage_collection_child_cas`, `stage_collection_aggregates`, `stage_retry_job`, `mark_interrupted_active_jobs`; `services/collection_persistence.py:stage_child_completion`; `collection_child_runner.py:run_collection_child` and placement/completion checks; QueueManager `request_cancel`/`wait_for_attempt`/`reset_attempt`; JobService cancel/retry and `job_runner.py:_fail`; `job_mapping.py` parent/child code-based retry mapping. Tests read: collection migration parity/legacy preservation; collection persistence eligibility, atomic identity/counters, rollback and waiting Delete File writer; collection child runner publish/caller-cancel and repeated-cancel drain; single retry stale-write/commit-failure/reset/drain tests; collection jobs real queue-guard regression. These establish existing boundaries, not coverage of the proposed durable parent lifecycle.

---

# 20. Cancellation

Cancellation must be real, not merely a UI state change.

For an active yt-dlp/FFmpeg subprocess:

1. mark cancellation requested,
2. send graceful process termination,
3. wait for a short grace period,
4. force terminate if required,
5. preserve appropriate partial data,
6. update job state to `cancelled`.

Retrying later may allow yt-dlp to continue from a valid `.part` file when supported.

For a collection, parent Cancel propagates to active child subprocesses and prevents not-started children from being scheduled. Completed children, finalized files, and Library entries remain untouched. Parent Retry includes failed, cancelled, and incomplete children only, preserving completed work. When cancellation races with child failure, terminal precedence is decided by durable persisted state: a Cancel request persisted before the parent's terminalization transaction makes the parent `cancelled`; a failure terminal committed first keeps the parent `failed`, and a later Cancel cannot supersede it. In-memory event timing is never the decision source. This mechanism does not define all-completed or success-versus-Cancel semantics or shutdown policy; the single runner's precedence is unchanged.

---

# 21. Restart Behaviour

SQLite state survives container restarts.

On application startup:

## Queued jobs

May remain queued and continue normally.

## Previously active jobs

Jobs that were:

- analyzing,
- downloading,
- post_processing,
- finalizing

when the application stopped should not be blindly assumed successful.

For v0.1, use failed/interrupted recovery unless the locked collection recovery branches below apply; single-job recovery remains unchanged. An interrupted job uses a retryable error such as:

```text
interrupted_by_restart
```

The user can retry them.

This is safer than attempting complicated automatic recovery during post-processing.

For locked collection parents, startup recovery follows the locked precedence recorded in §19 and PRODUCT §17 — implemented: without a marker, when the full nonempty selected child set is already committed with statuses only completed or failed, actual child facts win before normalization and settle the parent as `completed` (C = N, F = 0), `failed`/`all_children_failed` (F = N), or `failed`/`partial_failure` (C > 0, F > 0, C + F = N); with a persisted Cancel marker, the existing Cancel matrix stays authoritative. The incomplete branch above is preserved: an interrupted active child becomes `failed`/`interrupted_by_restart`, queued children are unchanged, and existing failed/completed children stay preserved. This exception covers only such selected completed/failed sets — not all terminal sets including cancelled — and introduces no queued/corrupt-set policy broadening, no new filesystem mutation/reset/callback/shutdown API, and no further change to current code behaviour.

For a normal orderly stop, the locked collection shutdown settlement recorded in §19 and PRODUCT §17 — implemented — commits its durable settlement before the existing engine dispose using the same startup recovery matrix; the startup fallback above stays unchanged after crashes. This note changes nothing for queued jobs or single-job recovery.

Future versions may improve automatic resume behaviour.

---

# 22. Database

Use SQLite for v0.1.

Recommended settings:

- WAL mode
- foreign keys enabled
- appropriate busy timeout

Schema changes must use migrations.

Use Alembic from the beginning.

Do not rely on automatic `create_all()` as the long-term migration strategy.

---

# 23. Suggested Database Model

## analysis_sessions

```text
id
owner_id
source_url
platform_hint
detected_platform
extractor
normalized_result_json
created_at
expires_at
```

## download_jobs

The model below is illustrative, not a finalized M5 schema. Use a migration to persist parent/child state, selection/order, and the collection-level mode/profile preference while preserving existing single-job records and API behaviour. Exact profile fields/enumerations and child resolution representation remain implementation design follow-ups; `selected_variant_id` below illustrates the existing single-item choice, not a universal collection source format.

```text
id
owner_id
analysis_id
source_url
title
job_type
selected_variant_id
destination
keep_app_copy
conflict_policy
status
progress_percent
total_items
completed_items
failed_items
error_code
error_message
created_at
started_at
completed_at
updated_at
```

## job_items

M5-2 persistence technical choice: revision `0005` adds `job_type` (`single` / `collection`), selected-item counters and nullable `collection_mode` (`audio` / `video`) plus `collection_profile_json` (opaque serialized preference, not a supported-profile matrix). A null profile means Original / Best Available; a null mode is unspecified, not an inferred audio/video default. Single jobs keep their required `variant_id`; collection persistence uses the empty string as an unused legacy-field placeholder, never as a child source format. Legacy rows default to single with structural counters `1/0/0`, regardless of status; these are not reconstructed completion facts and the single runner need not update them. No synthetic children or Library mutations are made. Children have an independent internal ID and snapshot `item_id`, raw positive `source_order`, nullable source ID/title and playlist/track/disc metadata, required source URL, and independent lifecycle/progress/output/error/timestamps. Identity and source order are unique within a parent; source IDs/metadata need not be unique. A cascading parent FK removes database child rows only, never files. The repository stages only new caller-owned parents/children, with no commit, and queries children via parent ownership in source order. Snapshot selection validation, 200-item enforcement, profile support, scheduling, CAS and aggregate lifecycle remain subsequent bounded tasks.

Persist only the selected children, with their independent state, progress, output, and error details. Validate ownership through the parent; parent creation and selected-child persistence must commit or roll back together. Child completion and Library finalization must retain the storage/catalog consistency guarantees in section 51, and retry/restart must not duplicate completed-child Library records.

```text
id
job_id
source_item_id
playlist_index
disc_number
track_number
title
status
progress_percent
output_path
error_code
error_message
created_at
updated_at
```

## library_items

Represents a logical downloaded media item.

```text
id
owner_id
job_id
source_url
source_id
platform
media_type
title
artist
album
album_artist
track_number
disc_number
year
thumbnail_path
format
codec
bitrate
resolution
duration
created_at
```

## media_files

Represents actual physical files.

This allows one logical item to exist in more than one destination.

```text
id
library_item_id
storage_type
path
display_path
size_bytes
checksum_optional
created_at
```

Possible `storage_type` values:

```text
app_library
plex_music
plex_video
```

## activity_events

```text
id
job_id
job_item_id nullable
level
event_type
message
details_json nullable
created_at
```

## app_settings

Store only application configuration that belongs in the database.

Sensitive secrets should not be stored here without an explicit future design.

---

# 24. Future Authentication Boundary

v0.1 has no login system.

However, API and service code should not hard-code the assumption that user ownership can never exist.

Introduce a lightweight request actor abstraction.

Example concept:

```text
Request
   ↓
get_actor()
   ↓
Actor(id="local")
```

For v0.1 all requests resolve to the local actor.

User-owned database records may use:

```text
owner_id = "local"
```

Later:

```text
get_actor()
```

can be replaced by authenticated identity resolution without rewriting core domain services.

Do not implement:

- login UI,
- password database,
- sessions,
- JWT,
- OAuth

in v0.1.

---

# 25. Storage Layout

Recommended container filesystem:

```text
/data/
├── state/
│   └── downloader.db
│
├── staging/
│   └── <job-id>/
│
└── library/
    ├── audio/
    └── video/

/media/
├── plex-music/
└── plex-videos/
```

Meaning:

```text
/data/state       persistent application state
/data/staging     incomplete/temp jobs
/data/library     App Library
/media/...        bind-mounted external Plex libraries
```

All should be configurable through deployment configuration.

---

# 26. Docker Mount Model

Example conceptual Compose configuration:

```text
Host                          Container

downloader state   ───────▶   /data/state
staging            ───────▶   /data/staging
app library        ───────▶   /data/library
Plex Music         ───────▶   /media/plex-music
Plex Videos        ───────▶   /media/plex-videos
```

Plex mounts require write permission for Downloader.

The application must fail clearly if a configured destination is not writable.

---

# 27. Display Paths

The path useful to a human may differ from the container path.

Therefore storage configuration should support an optional display-path mapping.

Example:

```text
Container path:
/media/plex-music/Led Zeppelin/Led Zeppelin IV

Display path:
/srv/media/music/Led Zeppelin/Led Zeppelin IV
```

The Library UI can show `display_path` when configured.

Otherwise it may show the container path.

This keeps Docker internals from becoming unnecessarily confusing to the user.

---

# 28. Staging Rules

Every download starts inside a job-specific staging directory.

Example:

```text
/data/staging/01JABC.../
```

yt-dlp and FFmpeg temporary output remains here until processing succeeds.

Plex directories must not receive:

- `.part` files,
- incomplete media,
- temporary artwork,
- failed conversions.

Only finalized media is transferred to the final destination.

---

# 29. Finalization

Finalization should be a dedicated stage.

```text
completed download
       ↓
metadata
       ↓
filename normalization
       ↓
conflict check
       ↓
copy/move to temporary final path
       ↓
atomic final rename where possible
       ↓
database update
```

If moving within the same filesystem, an atomic rename should be preferred.

If crossing filesystems:

1. copy to a temporary filename,
2. verify copy completion,
3. rename to final filename,
4. only then mark finalization complete.

A temporary destination file should use a suffix that Plex will not interpret as valid finished media.

---

# 30. File Conflict Handling

Supported policies:

```text
skip
replace
keep_both
```

Default:

```text
skip
```

## Skip

Keep existing file unchanged.

## Replace

Replace only after the new file has been successfully prepared.

Do not delete the existing file before the replacement is ready.

## Keep Both

Create a deterministic alternate filename.

Avoid random repeated names where possible.

Example:

```text
Track Name.ext
Track Name (2).ext
```

or another safe deterministic suffix.

The exact naming policy should remain centralized inside `StorageService`.

---

# 31. Music Path Builder

Music naming must be controlled by one centralized filename/path builder.

Primary pattern:

```text
Artist/
└── Album/
    └── NN - Track Name.ext
```

Example:

```text
Led Zeppelin/
└── Led Zeppelin IV/
    └── 04 - Stairway to Heaven.m4a
```

For multi-disc albums, disc information may be included in the track prefix when available.

The path builder must not depend on Plex being present.

M6 decisions (approved; planned, not implemented; PRODUCT_SPEC §13):

- Artist and album come only from explicit source or user metadata; never from the title, never the channel name as artist, and a playlist name is not automatically an album.
- Missing artist/album gives a flat audio layout without the missing folders; a missing track name falls back to the source title.
- `NN` for collections follows the original source playlist order, not a metadata track number, and numbers of unselected items are not compacted. This does not decide the embedded track-number tag.
- No automatic title cleanup; the user may edit the name manually.
- The cover selection (§34) may supply `cover.jpg` when an album folder exists.
- These rules go through the single centralized builder and `sanitize_media_filename` / `StorageService`; no new path logic elsewhere. Exact layouts for partial metadata are a design follow-up.

---

# 32. Video Path Builder

Standalone video:

```text
Title.ext
```

Playlist video:

```text
NN - Title.ext
```

Source IDs should remain database metadata rather than default visible filenames.

M6.3 (implemented): collection video files are named `NN - Title.ext` inside `StorageService` from the persisted child `source_order` (at least two digits, never compacted or truncated) and the persisted title; standalone video and audio filenames are unchanged. No automatic title cleanup; the title is the source title; manual name editing and audio numbering remain planned, not implemented.

---

# 33. Filename Safety

All path segments must be sanitized.

Requirements:

- preserve useful Unicode where possible,
- normalize Unicode consistently,
- remove `/`,
- remove NUL,
- remove unsafe control characters,
- prevent `..` traversal,
- trim invalid trailing characters,
- enforce reasonable maximum filename lengths,
- provide deterministic fallback names.

Never use an untrusted media title as a raw filesystem path.

All resulting paths must be verified to remain within the configured destination root.

---

# 34. Metadata Confidence

Downloader should distinguish between:

```text
source-provided metadata
derived metadata
user-edited metadata
unknown metadata
```

Do not aggressively infer:

```text
Artist - Song Title
```

from arbitrary video titles and treat it as guaranteed truth.

When metadata is uncertain, the UI may allow editing before starting the download.

This is especially important for Plex Music destinations.

M6 artwork decision (approved; planned, not implemented; supersedes the earlier medium/low-only rule): every artwork candidate, at any confidence and from any source including a trusted one, is previewed and requires explicit user selection; explicit selection makes it user-confirmed artwork. There is no silent embedding. With no selection the download continues without artwork. A selection is used for embedding in supported audio formats and for `cover.jpg` when an album folder exists; format support is not guaranteed and details come later. A single cover is not applied automatically to all tracks of a playlist with different albums. Selecting a cover does not change other metadata by itself.

Alternative cover search (approved; planned, not implemented): triggered only by the user pressing the `Alternative cover search` button, with editable artist/album, an external catalog search, candidates shown with source/release information and, where verifiable, resolution, and a user choice. High pixel size is not proof of the correct release. No silent search, selection or replacement. A failed search or missing cover never blocks the download. This is the last separate M6 sub-task, not started; provider and rights terms must be clarified first, and it is not deferred or completed automatically. MusicBrainz + Cover Art Archive is only the first provider candidate and a single chain, not two independent catalogs; it is not a final provider decision and no integration exists. No new provider framework, plugin system or separate service is planned.

Rights: the application will be distributed as open source; code, data and image licenses are separate, open source does not automatically mean non-commercial, and covers are not bundled with the software package. API access or user selection does not replace copyright permission; external preview/cache/embed rights are not yet verified, and the research is not full legal approval. Official terms: https://musicbrainz.org/doc/MusicBrainz_API , https://musicbrainz.org/doc/About/Data_License , https://musicbrainz.org/doc/Cover_Art_Archive , https://metabrainz.org/supporters/account-type .

---

# 35. Keep a Copy in App Library

A Plex destination plus:

```text
Keep a copy in App Library
```

means Downloader should produce another actual file in App Library.

The database architecture supports this through multiple `media_files` records connected to one logical `library_item`.

Do not create duplicate `library_items` for the same logical downloaded media solely because it exists in two storage locations.

---

# 36. Download to This Device

The server always prepares the requested final media first.

The frontend then downloads it through an HTTP endpoint.

Conceptually:

```text
GET /api/v1/library/{item_id}/download
```

or a short-lived equivalent.

Requirements:

- use `Content-Disposition`,
- expose the correct filename,
- avoid exposing arbitrary filesystem paths,
- support large files,
- support HTTP range/resume behaviour where practical.

The browser handles final saving to the phone/computer.

---

# 37. Library Model

The Library is not merely a directory listing.

It is a database-backed catalog of media Downloader knows about.

Filesystem access should go through Library/Storage services.

The frontend must never receive arbitrary delete-file endpoints that accept filesystem paths.

Deletion uses media/library IDs.

---

# 38. Delete Behaviour

## Remove from Library

Delete the database catalog record as defined by product behaviour.

Do not delete the physical file.

## Delete File

Requires explicit confirmation.

The backend resolves known physical files through the database.

The client must never submit:

```text
/path/to/delete
```

as an unrestricted filesystem deletion request.

The physical target is resolved only from the catalog's storage mapping plus delete roots captured at startup: after the storage layout exists, the backend captures each root's device/inode identity — the App Library root mandatorily, configured Plex roots only when present, so a missing optional root leaves deletions unavailable (fail-closed) without taking startup down. The filesystem root itself is never a delete root, and no root descriptors are cached.

Startup snapshots the configured root's absolute lexical walk and canonical path, including the identity and link text of existing aliases (such as macOS `/var` → `/private/var`). Every root operation replays those saved components from the filesystem root descriptor with `O_DIRECTORY|O_NOFOLLOW`, never re-resolving the current configured path; startup aliases must remain unchanged and their saved expansions are walked without following the current link. The final descriptor's identity must match the startup capture — an ancestor or root replaced by a new symlink fails closed even when it leads to the same startup root inode. Reference resolution, writer verification, and deletion use this same policy. From that validated root descriptor the catalog path is walked the same way and the final entry is pinned: any symlink on the path is rejected, traversal and root/directory targets are rejected, and the unlink runs under the held parent descriptor, so parent or root swaps cannot redirect the deletion outside a root. Within the same parent directory POSIX has no atomic verify-and-unlink, so a same-directory entry swap in the micro-window between the identity recheck and the unlink is the documented residual race (it cannot leave the root, and every detectable swap fails closed).

Reference resolution carries both the storage-resolved location and, for an existing entry, its `(st_dev, st_ino)` under the same validated-root semantics. A matching location or existing file identity counts as a shared reference, including different case spellings or storage mappings reaching the same physical target; safely missing paths retain only their lexical location. A shared — or unverifiable (unavailable root, symlinked or unsafe path) — reference blocks deletion with a stable, non-retryable conflict and leaves the database and the file unchanged.

Ordering: immediately after the owner/item/copy validation, stage the copy's catalog delete (this takes the SQLite write lock), run the catalog-wide reference scan inside that same transaction, then delete the physical file through the prepared handle and commit. A concurrent same-path catalog write therefore either serializes before the scan (visible → blocked) or cannot commit until the transaction ends; the irreversible steps (target preparation, unlink, commit) run shielded so cancellation cannot leave the file/database outcome undecided. Storage failures roll the catalog back; database failures before the unlink map to stable retryable busy/database errors with nothing changed; a rare commit failure after the file is gone returns a distinct catalog-consistency error instead of a silent success (§51).

The serialization is completed by the writer side: every catalog write that commits a `media_files` row must already hold the SQLite write lock and re-verify the physical target's existence and placement identity through the storage boundary before committing (§51). A Delete File that committed while the writer waited on the lock is caught by that re-verification — the writer's whole transaction, the job completion transition included, rolls back instead of committing a dangling reference or a completed job whose file is gone. A writer that commits first is visible to the delete's reference scan and blocks the delete as a shared reference without mutation; writers of different paths are unaffected.

If multiple physical copies exist, the UI should make the target clear.

---

# 39. Activity Event Architecture

Activity must be structured.

Do not expose raw stdout/stderr directly as the primary UI log.

Example internal event:

```json
{
  "id": 187,
  "job_id": "01J...",
  "level": "info",
  "event_type": "download_progress",
  "message": "Downloading track 4 of 8",
  "details": {
    "percent": 61.4,
    "speed": 7340032
  },
  "created_at": "..."
}
```

The event database supports:

- UI history,
- reconnect,
- debugging,
- future Detailed mode.

---

# 40. Live Updates: SSE

Use Server-Sent Events for v0.1.

Preferred endpoint:

```text
GET /api/v1/events
```

Why SSE:

- browser support is simple,
- communication is primarily server → client,
- automatic reconnect behaviour,
- sufficient for progress/activity,
- less complexity than WebSocket.

User actions such as:

- cancel,
- retry,
- create job

remain normal HTTP API calls.

There is no v0.1 requirement for bidirectional socket communication.

---

# 41. SSE Event Types

Possible event categories:

```text
job.created
job.updated
job.completed
job.failed
job.cancelled

job_item.updated

activity.created

library.created
library.updated
library.deleted
```

Example:

```text
event: job.updated
id: 188
data: {...}
```

The frontend should update its query cache/state from these events.

---

# 42. SSE Reconnection

Activity event IDs should be monotonically increasing database IDs.

Where practical, support:

```text
Last-Event-ID
```

so reconnecting browsers can retrieve missed events.

A full sophisticated message broker is not required.

If replay complexity becomes excessive during the initial vertical slice, the frontend may reconnect and refresh authoritative REST resources.

---

# 43. Error Model

API errors should use a consistent structure.

Example:

```json
{
  "error": {
    "code": "extractor_failed",
    "message": "The selected extractor could not analyze this URL.",
    "retryable": true,
    "job_id": null
  }
}
```

Possible error codes:

```text
unsupported_url
extractor_failed
authentication_required
geo_restricted
media_unavailable
network_error
invalid_format
destination_unavailable
destination_not_writable
file_conflict
ffmpeg_failed
download_failed
cancelled
interrupted_by_restart
internal_error
```

Frontend text should be user-friendly.

Technical information belongs in Activity/server logs.

---

# 44. Log Redaction

Before exposing any process output to Activity:

- remove credentials,
- remove cookies,
- remove authorization headers,
- remove tokens,
- avoid leaking future authenticated-source data,
- avoid unnecessarily exposing sensitive environment variables.

This should exist even before cookie support is implemented.

---

# 45. Platform Selection Behaviour

Frontend default:

```text
Auto Select
```

The backend accepts:

```text
platform_hint
```

Possible behaviour:

## Auto Select

No forced extractor hint.

Let yt-dlp determine the source.

## Manual selection

Treat the platform selection as an explicit hint/constraint.

If detected source clearly disagrees, return a normalized mismatch response rather than producing a cryptic error.

The frontend can offer:

```text
Use detected platform
```

---

# 46. No Extractor Health Service

Do not build:

- cron health checks,
- polling,
- extractor uptime database,
- last-success indicators,
- synthetic platform requests.

Actual analysis attempts provide enough information.

Failures generate normal application errors and Activity events.

---

# 47. Settings Architecture

Some settings are deployment-level and should remain environment variables.

Examples:

```text
database location
staging root
App Library root
Plex mount roots
server host/port
```

Runtime user preferences may be database settings.

Examples:

```text
max concurrent jobs
default conflict behaviour
default destination
```

Do not allow the browser to arbitrarily configure unrestricted host filesystem paths.

Docker mount configuration remains an administrator/deployment concern.

---

# 48. Suggested Environment Configuration

Conceptual variables:

```text
DOWNLOADER_STATE_DIR=/data/state
DOWNLOADER_STAGING_DIR=/data/staging
DOWNLOADER_LIBRARY_DIR=/data/library

DOWNLOADER_PLEX_MUSIC_DIR=/media/plex-music
DOWNLOADER_PLEX_VIDEO_DIR=/media/plex-videos

DOWNLOADER_PLEX_MUSIC_DISPLAY_PATH=
DOWNLOADER_PLEX_VIDEO_DISPLAY_PATH=

DOWNLOADER_PORT=8080
```

Exact names may be refined during implementation.

---

# 49. Security Rules

Even though the service is initially private:

1. Never construct shell command strings from URLs.
2. Never use user strings as unrestricted filesystem paths.
3. Never expose delete-by-path APIs.
4. Validate destination roots.
5. Validate selected variants against the stored analysis.
6. Sanitize filenames.
7. Redact activity logs.
8. Validate request payloads through Pydantic.
9. Do not expose the application publicly without adding proper authentication/security design.
10. Treat metadata returned from remote websites as untrusted input.

---

# 50. SSRF Consideration

Downloader intentionally accepts remote URLs, so URL fetching is a core product feature.

However, arbitrary URL handling can interact with private/internal network resources.

The architecture should keep URL handling isolated inside the yt-dlp integration layer.

v0.1 should at minimum reject obviously unsafe unsupported schemes such as:

```text
file://
ftp://
unix://
```

Normal expected schemes:

```text
https://
http://
```

More advanced SSRF policy can be added if the application is ever exposed beyond a trusted private network.
---

# 51. Database/Filesystem Consistency

The filesystem and database can fail independently.

Therefore finalization must follow explicit ordering.

Recommended pattern:

```text
prepare file
 ↓
successfully place final file
 ↓
verify expected file exists
 ↓
write/update library database
 ↓
mark job completed
```

If the database update fails after file creation, the job must not silently report success.

The "verify expected file exists" step runs inside the catalog-write transaction, after the SQLite write lock is held: the placed file's storage identity is re-verified through the storage boundary right before the catalog commit. A concurrent Delete File that removed the file while the writer waited on the lock fails that re-verification and the whole completion rolls back; a delete that runs afterwards sees the committed row as a shared reference and is blocked (§38).

A reconciliation mechanism may be added later if needed.

---

# 52. Cleanup Policy

Successful job staging directories should be cleaned after finalization.

Failed/cancelled jobs may temporarily preserve staging files for:

- retry,
- resuming partial downloads,
- debugging.

v0.1 should define a conservative cleanup policy.

For example:

- successful staging: clean immediately,
- failed/cancelled staging: retain for a configurable period or until retry/removal.

Do not allow staging to grow forever without visibility.

Detailed automatic retention management may be added after the first vertical slice.

---

# 53. Testing Strategy

Tests should be layered.

## Unit Tests

Test:

- filename sanitization,
- path generation,
- metadata normalization,
- format normalization,
- state transitions,
- conflict policies,
- error mapping.

## Service Tests

Test:

- job creation,
- queue behaviour,
- cancellation,
- retry,
- finalization,
- Library operations.

Use fake yt-dlp and FFmpeg adapters.

## API Tests

Test important endpoints with FastAPI test tooling.

## Frontend Tests

Test:

- Analyze form,
- format selection,
- destination selection,
- queue states,
- Activity expansion,
- Library actions.

## Integration Tests

Run yt-dlp/FFmpeg against controlled local fixtures where possible.

CI should not depend on YouTube or another external website remaining available.

Real-platform tests should primarily be manual/smoke tests.

---

# 54. External Website Testing Rule

Do not write CI tests that fail merely because:

- YouTube changed,
- SoundCloud is rate-limiting,
- an internet connection is unavailable,
- a test video was removed.

The yt-dlp integration boundary must be mockable.

A manual smoke-test checklist can validate real extractors before release.

---

# 55. Quality Gates

Before an implementation milestone is considered complete:

Backend:

```text
lint
typecheck
tests
```

Frontend:

```text
lint
typecheck
tests
build
```

Deployment:

```text
Docker image builds successfully
container starts
health endpoint succeeds
```

The agent saying "done" is not sufficient.

---

# 56. Health Endpoint

The backend should expose a lightweight application health endpoint.

Example:

```text
GET /api/v1/health
```

It may validate:

- API process is alive,
- database is accessible.

It must NOT contact media platforms as part of health checking.

Optionally, readiness may verify required local directories.

---

# 57. Docker Healthcheck

Docker may call the local health endpoint.

This only measures Downloader itself.

It must not trigger:

- YouTube requests,
- extractor tests,
- Plex scans,
- external network probes.

---

# 58. Initial Vertical Slice

The first implementation milestone is deliberately smaller than the full v0.1 product.

Implement:

```text
React UI
   ↓
Paste URL
   ↓
POST /analysis
   ↓
yt-dlp metadata analysis
   ↓
Normalized source/format response
   ↓
Select format
   ↓
Create job
   ↓
Persistent SQLite queue
   ↓
yt-dlp download
   ↓
SSE progress/activity
   ↓
App Library finalization
   ↓
Library page
```

For the first vertical slice:

- App Library only
- single media item only
- Auto Select
- basic video/audio options
- basic Activity
- basic queue
- no playlist yet
- no Plex yet
- no Download to Device yet

The architectural foundation must nevertheless allow those features to be added next.

---

# 59. Implementation Milestones

Recommended order:

## Milestone 0 — Foundation

- repository scaffolding
- backend application
- frontend application
- Docker build
- SQLite
- migrations
- health endpoint
- quality tooling

## Milestone 1 — Analysis

- URL form
- Auto Select
- yt-dlp analysis adapter
- normalized metadata
- normalized formats
- analysis session persistence
- error handling

## Milestone 2 — First Download

- job creation
- persistent queue
- one active download
- App Library
- progress
- cancellation
- Activity events

## Milestone 3 — Library

- Library page
- media details
- sizes
- dates
- paths
- Remove from Library
- Delete File

## Milestone 4 — Queue Hardening

- configured concurrency
- retry
- restart handling
- multiple queued jobs
- failure states

## Milestone 5 — Playlist / Album

Scope: selectable playlist/album items, persisted parent/child jobs, one collection-level mode/profile, source-aware child execution, aggregate progress/counters, and source track ordering. App Library remains the destination for this milestone. M6 naming/metadata/artwork, M7 Plex destinations, and M8 device delivery remain out of scope, as do entire channel/profile bulk downloads, per-item output profiles, per-child Cancel / Retry UI, and existing Library bulk conversion/reprocessing.

Implement in dependency-ordered bounded tasks, preserving single-item behaviour at every step. The focuses below describe acceptance and targeted validation, not a finalized payload/schema/profile enumeration:

1. **Selectable collection analysis and 200-item guard.** Expose analyzed selectable items, ordering, and clear selection-limit information without treating source totals as capped at 200. Validate the backend selected-item hard cap and truthful Select All scope. Target normalizer/analysis/API/adapter tests for selectable identity/order, available-selection scope, limit boundaries, and large-source information. Decide retrieval and identity/duplicate/stale-selection handling explicitly during technical design; do not silently prescribe pagination or all-source retrieval.
2. **Migration and parent/child/profile persistence.** Persist parent/child relationships, selected ordering, independent child lifecycle/error/output data, and the collection-level mode/profile preference. Target migration/model parity, existing single-job compatibility, persistence, ownership relationships, and atomic rollback tests. Do not finalize profile enumerations or low-level schema here.
3. **Validated create/read contract.** Validate owned, unexpired analyses, selectable item references, nonempty selection, the 200-selected-item cap, and allowed collection-level preferences. Create the parent and selected children atomically; reads enforce owner isolation. Target empty/duplicate/unknown/stale selection handling under the agreed identity contract, expired analyses, owner isolation, rollback, and single-item API regressions.
4. **Source-aware profile resolution, child execution, aggregate progress, and Library.** Resolve Original / Best Available or explicit conversion per child through integration adapters, preserving actual source limits and accepting heterogeneous native outputs. Conversion must affect only selected children of the new job. Finalize each successful child into App Library under existing storage/catalog consistency rules and preserve source ordering and truthful counters. Target resolver, controlled adapter/runner, aggregate, finalization, and Library tests for heterogeneous sources, video ceilings/no upscale, conversion scope, and no duplicate records. Agree aggregate-percent and scheduling/concurrency-slot semantics before execution changes; do not treat them as settled product policy.
5. **Partial failure, cancel/retry, and restart hardening.** Mixed completed/failed terminal results use parent `failed` with `partial_failure` error semantics and preserved child reasons/counters; there is no partial-success state. Parent cancellation stops active and not-started work; retry runs only failed/cancelled/incomplete children. Restart recovery preserves completed outputs/Library entries and does not repeat them. Target lifecycle/race/restart tests, completed-child preservation, idempotent finalization, and relevant M4 queue regressions. All-failed aggregation and cancel/failure precedence are decided product policy (PRODUCT_SPEC section 17, ARCHITECTURE section 19): parent `failed` with stable `all_children_failed` semantics, Retry only with a retry-eligible failed/cancelled/incomplete child, and durable-state precedence where a committed failure terminal is not superseded by a later Cancel; implement and test these decided contracts rather than escalating them.
6. **UI selection, collection profile, and progress.** Offer selectable items, scoped Select All / Deselect All, explicit large-source v0.1 limit messaging, one Audio / Video and output profile choice defaulting to Original / Best Available, and real aggregate counters/child failure information. Expose parent-only Cancel / Retry. Target New Download/Queue/Activity tests for selection/profile reset, cap boundaries and no hidden selection, source-quality/conversion messaging, mixed results, completed-child preservation, and SSE/poll update guards.
7. **Independent review and final gates.** Independently review the integrated milestone, resolve findings in bounded tasks, then run fresh configured full gates on the current tree under Lead/Checker ownership. Implementation tasks run only their relevant targeted checks; documenting this breakdown does not authorize implementation or establish milestone acceptance.

## Milestone 6 — Metadata and Naming

Approved decisions below are planned, not implemented (see §14, §31, §32, §34).

- source-quality audio handling: lossless WebM/Opus → `.opus` remux, AAC/M4A kept, no default MP3/AAC re-encode, no reprocessing of past Library items
- required conversion for new single and collection downloads (approved; planned, not implemented): audio Original / Best Available (default), MP3, WAV, M4A, Opus, FLAC with fixed profiles and no slider; video Original and MP4 (compatible, real H.264/AAC). One chosen output per job/collection; library conversion, file upload and per-child outputs out of scope. Failing explicit target fails the job/child; no silent original publish. Rules: source selection separate from output choice; existing FFmpeg/prepared_media/staging/storage boundary, no separate converter/runner. Sub-tasks:
  - M6.4a decision/spec (this document update)
  - M6.4b output-profile persistence/API: selection durable on the job, retry reuses it, missing/old profile reads as Original
  - M6.4c FFmpeg adapter conversion support
  - M6.4d conversion source resolution/pipeline with cancel/retry
  - M6.4e UI
  - M6.4f real-binary smoke with ffprobe codec/container verification
  M6.1 source_metadata, M6.2 path planning and M6.3 video numbering stay implemented; M6.4b–f are not started and M6 is not complete.
- artist/album paths from explicit source/user metadata only; flat audio layout when missing; source title as fallback track name
- embedded metadata, written on staged files
- source playlist order numbering for collection audio and playlist video names, not compacted
- no automatic title cleanup; manual metadata/name review and editing
- artwork preview with explicit user selection for every candidate (no silent embedding, no selection means no artwork), embedding where supported and `cover.jpg` with an album folder
- tag/artwork/remux failure fallback (PRODUCT_SPEC §14, ARCHITECTURE §14): under Original, a remux failure keeps the safe original with an Activity warning; when an explicitly selected target was already converted successfully, optional tag/artwork failure keeps that ready target; if the explicitly selected target cannot be produced, the job/child fails and no different original is completed; cancel/security/storage/catalog errors not swallowed
- last separate sub-task, not started and gated on provider/rights clarification: user-triggered alternative cover search (MusicBrainz + Cover Art Archive as the first candidate, single chain, not a final choice)

## Milestone 7 — Plex Destinations

- Plex Music
- Plex Videos
- staging → final transfer
- destination validation
- Keep a Copy in App Library

## Milestone 8 — Device Download

- browser file delivery
- large-file behaviour
- download filename handling

## Milestone 9 — v0.1 Polish

- responsive mobile UI
- error messages
- Settings
- conflict policies
- end-to-end smoke tests
- documentation

## v0.1 Release Security Hardening / Audit

One-time release security stage after Milestone 9 feature completion/polish and before final v0.1 release acceptance and gates. Status: NOT STARTED / planned. Documenting this stage does not authorize starting it, is not execution or acceptance, and does not change the current milestone plan or the next bounded task.

This stage is not a new feature, not post-v0.1 scope, not a public-exposure or authentication change, and not continuous security infrastructure. The current private Tailscale / no-auth assumption (PRODUCT_SPEC section 24) and the security rules of sections 49–50 remain unchanged. No recurring telemetry, dashboards, metrics, or recurring audit infrastructure is introduced.

Audit execution model:

- The initial audit is read-only and performed by an existing reviewer role (see `docs/WORKFLOW.md`); no new agent/role is added and the auditor applies no fixes directly.
- Every finding includes evidence, affected boundary/files, realistic preconditions and impact, severity rationale, and a proposed mitigation.
- An auditor severity label alone is not acceptance: meaningful findings are triaged by an independent reviewer from a different model family than the auditor before any implementation.
- False positives are explicitly dismissed with evidence, never silently dropped.
- Validated fixes are separate bounded implementation tasks under the normal routing and cross-family review of `docs/WORKFLOW.md`.
- Finding dispositions are recorded, and unresolved validated Critical/Important findings are v0.1 release blockers. Fixes receive targeted re-review where the fix requires it; this is not a continuous or per-task audit.
- Full normal gates are not run when the audit starts; the fresh configured full gates (section 55) run once at the final release gate. Use the existing configured quality gates; do not invent new validators.

Bounded workstreams:

1. Ingress/trust boundaries: URL input handling; SSRF and redirects (section 50); yt-dlp source validation; `variant_id` / collection item IDs; raw path and raw CLI argument boundaries.
2. Subprocess: `create_subprocess_exec` / shell boundary; yt-dlp/FFmpeg argument construction; process groups; cancellation/termination; environment; staging/temp handling.
3. Filesystem: path traversal; symlink/inode/TOCTOU; placement/finalization ordering (section 51); conflict behaviour; Delete File; hardlink/same-file handling; Library containment.
4. State/DB/concurrency: transactions/CAS; retry/cancel races; restart recovery; stale callbacks/attempt ownership; duplicate finalization; collection parent/child consistency.
5. Authorization/ownership: owner scoping; cross-job/library access; object ownership; current Tailscale/no-auth assumptions; future-auth boundary risks (documented only; no authentication implementation).
6. Frontend/API: forged IDs; malformed/oversized payloads; backend validation; SSE exposure; error and information leakage.
7. Dependencies/container/deployment: Docker runtime/user; mounts; secrets/config; exposed ports; reverse proxy; dependency/runtime vulnerability exposure.
8. Abuse/resource exhaustion: large/pathological collections/media; disk exhaustion; queue flooding/concurrency; oversized metadata/artwork; long-running subprocesses; restart/resource loops.
9. Final security regression and release gate: validate resolved findings; targeted security regressions; fresh configured normal full gates; deployment/release security checklist.

Do not implement all milestones in one agent task.

---

# 60. Future Architecture Hooks

The architecture should allow—but not implement yet—the following.

## Authentication

Replace local actor provider with real identity provider.

## Multi-user

Scope jobs/library by `owner_id`.

## Cookie Support

Add encrypted credential/session storage and sanitized yt-dlp cookie integration.

## Advanced Activity

Expose structured detailed/raw-ish sanitized logs.

## Separate Worker

QueueManager can later move out of the API process.

Conceptually:

```text
API
 ↓
durable queue
 ↓
worker
```

Only introduce Redis/Celery/etc. if that migration becomes necessary.

## PWA Share Target

A shared URL can pre-fill the New Download form.

## Plex API

A completed Plex destination could optionally trigger a library scan.

---

# 61. Architectural Non-Goals for v0.1

Do not optimize for:

- distributed workers,
- cloud-scale deployment,
- thousands of simultaneous users,
- multiple backend replicas,
- Kubernetes orchestration,
- public anonymous access,
- microservices.

The architecture should be clean enough to evolve without paying the complexity cost before it is needed.

---

# 62. Key Architectural Decisions

The following decisions should be treated as accepted unless deliberately revisited:

```text
Deployment:
Single Docker application container

Backend:
FastAPI / Python

Frontend:
React / TypeScript

Database:
SQLite + migrations

Queue:
Persistent SQLite records + in-process QueueManager

Concurrency:
Configurable, conservative default

Live updates:
SSE

Media engine:
yt-dlp + FFmpeg

yt-dlp boundary:
Managed subprocess

Temporary files:
Dedicated staging directory

Final storage:
App Library / Plex mounts

Authentication:
None in v0.1, actor boundary prepared

Platform:
Auto Select default + manual hint

Extractor checks:
Only during real requests

Activity:
Structured application events

CI:
No dependency on live media platforms
```

---

# 63. Architecture Rule for Agents

When implementing a feature:

1. Read `PRODUCT_SPEC.md`.
2. Read the relevant section of this document.
3. Do not add infrastructure merely because it is common in larger systems.
4. Keep yt-dlp and FFmpeg behind integration boundaries.
5. Keep filesystem behaviour behind `StorageService`.
6. Keep queue state persistent.
7. Do not bypass domain validation from frontend requests.
8. Add tests for new business behaviour.
9. Do not silently change accepted product behaviour.
10. If a requirement seems technically problematic, explain the tradeoff before replacing it with a different design.

The goal is a small, understandable, reliable application—not maximum architectural sophistication.
