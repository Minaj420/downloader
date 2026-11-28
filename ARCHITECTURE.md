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
```

Superseded (approved; user-settled; PRODUCT_SPEC §17): there is no per-job `DELETE /api/v1/jobs/{job_id}` and no individual job/history deletion; history is removed only by the bulk Clear History action. The user settled the earlier spec-audit `DELETE /jobs` note in favour of this bulk-only decision; no per-job endpoint will be added and none exists in code. The bulk Clear History preview/confirm endpoints are specified in the Q3 technical contract (§59 M6-Q; planned, not implemented); no bulk backend exists yet.

`GET /api/v1/jobs` scoped listing (`scope=active|history`, cursor) is implemented in the backend and the frontend client/Queue page (Q1; current status: STATE.md). The contract lives only in §59 M6-Q (Q1 technical contract).

```text
GET    /api/v1/library
GET    /api/v1/library/{item_id}
DELETE /api/v1/library/{item_id}
DELETE /api/v1/library/{item_id}/file

GET    /api/v1/settings
PATCH  /api/v1/settings

GET    /api/v1/events
```

Names may change slightly during implementation, but REST semantics should remain clear.

Settings endpoints status (approved plan; §47, §59): read-only `GET /api/v1/settings` (S1) is approved and planned at the M7 opening, not implemented. `PATCH /api/v1/settings` and any DB-backed settings (S2a/S2b/S2c) are post-v0.1 future backlog (§60); until then settings come from Compose/environment.

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

M6 planned (not implemented): required audio profiles are Original / Best Available (default), MP3, WAV, M4A, Opus and FLAC, each with one fixed quality profile and no bitrate slider; required video profiles are Original and MP4 (compatible).

Fixed conversion encoding profiles (approved; this paragraph is the single authoritative parameter list, implemented as backend argument arrays): MP3 = LAME VBR V0 (`libmp3lame`, `-q:a 0`); M4A = AAC 256 kb/s; Opus = 160 kb/s with VBR (`libopus`); WAV = 16-bit PCM (`pcm_s16le`); FLAC = 16-bit (`-sample_fmt s16`); MP4 = x264 CRF 20, preset medium, with AAC 192 kb/s audio. These are encode settings applied only when a stream is actually encoded; they are not guarantees about streams that are copied/remuxed (existing compatible copy is kept, e.g. an already compatible audio or video stream is copied rather than re-encoded) or about `Original` output, and they are not a quality upgrade: conversion remains a compatibility choice and never restores quality absent from the source.

M6.4b foundation (implemented): wire field `output_profile` on job create (common to single and collection, outside the single/collection exclusion) and on `JobDto` (default `original`), values `original|mp3|wav|m4a|opus|flac|mp4`; audio set Original+MP3/WAV/M4A/Opus/FLAC, video set Original+MP4, chosen by the single variant kind or the collection mode. Unknown values are a 422 request error. Migration `0007` adds nullable `download_jobs.output_profile` (no default/backfill/CHECK); NULL and Original are the same and read as `original`; other stored values are returned unchanged and unknown ones fail mapping instead of falling back. One parent-level column; children have no output field, and `CollectionProfile` stays the native source-quality preference (`max_height`). Retry/reset/recovery keep the stored value. Conversion gate (M6.4d d1–d4): create accepts a known non-Original profile from the request's set; a single job also validates the runner's pure conversion plan first, so an incompatible source is rejected (`invalid_profile`/`invalid_variant`) without side effects, while collection source compatibility is checked per child after its probe. The single runner (after claim) and each collection child (before probe) fail an unknown stored value, the child also a known value outside its mode's set, with non-retryable `invalid_profile` before analysis/download/storage; no silent Original fallback, and existing parent settlement and cancel precedence are unchanged. The New Download UI (M6.4e) always sends an explicit `output_profile` (default `original`) in both create branches: the single variant's kind or the selectable collection mode picks the audio or video set, and a kind change or a new analysis resets the choice to Original.

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
- audio conversion and remux,
- artwork embedding (planned, not implemented; M6.7),
- other final media processing.

M6.5 text tags are not an FFmpeg responsibility (approved; implemented for single audio downloads in M6.5-T3 and audio collection children in M6.5-T4): they go through a thin Mutagen integration boundary (MP3 ID3v2.4, M4A MP4 atoms, FLAC / Ogg Vorbis / Opus Vorbis comments; no hand-written format code). FFmpeg stays for conversion/remux, and no second parallel tag engine is opened. Tagging works on a separate candidate copy in staging, which is re-read and verified. Two publish branches: (1) tagging and re-read verification succeed: the verified candidate is published through the normal `StorageService` containment/conflict/catalog path; (2) tagging fails: the failed candidate is never published, and the sound untagged prepared output from before tagging is published through the same normal storage/catalog checks with an Activity warning. Cancellation is re-checked before and after tagging. Single-download wiring: the runner tags after `prepare_media` and before placement, using `intended_text_tags` of the normalized source metadata/title; non-audio, no-tags, non-taggable suffixes (WAV, WebM) and the Original WebM remux fallback skip without a copy, and content the writer reports unsupported is published unchanged without a warning. Collection-child wiring applies the same gate after `prepare_media`: tags come from the child's own probe normalized with `normalize_source_metadata` (source track first) with the persisted child title as the source-title fallback, never playlist order or an invented title; the edit uses the child staging key and `place_child_final(source_identity=...)` binds its no-follow pin the same way while still publishing an independent copy; the fixed warning carries `child_id` in its details and reaches the caller-owned sink only after commit. `StorageService.stage_edited_copy` pins the prepared file with the child no-follow walk, creates an exclusive no-follow copy with the same suffix in that job's own staging directory (a real copy, never a hard link), lets the writer edit only the copy, rechecks the source and copy identities on every outcome and removes only its own copy. It returns the chosen inode still pinned open: the verified copy, or the rechecked original when nothing was written (unsupported content or a typed `TagError`, which the runner's editor adapter captures; storage does not import Mutagen). The blocking worker owns the descriptors and the copy; repeated waiter cancellation drains it, discards whichever result emerged and re-raises. Placement re-walks from the staging root and binds its pin to that identity (`source_identity`) while the chosen inode stays pinned, so a replacement cannot reuse its inode number and fails closed; the pin is closed only after placement and cleanup, and only an own unpublished copy is ever unlinked. Only a typed `TagError` selects branch (2); I/O, cleanup, security, storage and catalog errors fail the job. The fixed warning is written in the completion transaction after the catalog re-check and published only after commit. Pinned inodes may still be modified in place by a concurrent writer; this is not a guarantee against arbitrary filesystem mutation. A ready explicit MP3/M4A target never reverts to the old pre-conversion source format, and if the explicit target cannot be produced the job/child fails. Cancellation, storage, security and catalog errors are not swallowed. The narrow Original `EXIT_STATUS` remux fallback below is unchanged. WAV, the Original WebM fallback, video and other unsupported containers are outside tagging. Mutagen is GPL-2.0-or-later: no blocker for personal use, a distribution/project-license compatibility note remains open, and this is not legal approval.

Where yt-dlp safely handles a required FFmpeg postprocessor itself, that is acceptable.

However, application-level metadata and naming rules must remain controlled by Downloader.

FFmpeg processes should use argument arrays, not shell-concatenated command strings.

M6 decisions (approved; PRODUCT_SPEC §8, §14; explicit conversion and Original Opus remux are implemented; text tagging is implemented for single audio downloads and audio collection children, while artwork remains planned): Original / Best Available keeps source quality. WebM-contained Opus audio is remuxed losslessly into `.opus`; AAC/M4A is kept; MP3/AAC re-encoding is not a default and happens only for an explicitly selected output; past Library items are not reprocessed. Tagging, artwork and remux run on staged files only. If tagging or artwork fails under Original, the safe original is kept and the job completes with an Activity warning; cancellation, security, storage and catalog errors are not swallowed by this fallback. Remux failure is not covered by this sentence; it is governed only by the "Original Opus remux fallback" paragraph below. Staging and storage containment boundaries are unchanged.

Explicit conversion (approved; implemented in M6.4b–d via `prepared_media.py` and `FFmpegProcessor.convert`): conversion runs through this FFmpeg adapter and the existing prepared_media/staging/storage boundaries; there is no separate converter or runner. An explicitly selected output that cannot be produced fails the job/child; no silent publish of the original in a different format. Optional tag/artwork failure never reverts a ready target to the pre-conversion source format. Missing/unsupported encoder error handling is later technical design; no encoder is guaranteed per FFmpeg build. MP4 (compatible) requires real H.264/AAC output (copy/remux of compatible streams, transcode otherwise). Current runtime (`prepared_media.py`, `integrations/ffmpeg.py`) downloads, merges streams with copy and performs the explicitly selected conversion through `FFmpegProcessor.convert`; FFmpeg does no tagging or artwork (single-download and audio collection-child text tags go through the Mutagen boundary above; artwork remains planned). Original Opus remux is implemented in M6.5b/c through the same `FFmpegProcessor.convert` audio copy path. Historical M5 contracts that rejected conversion/accepted native-only remain a record of M5 and are superseded for new downloads by the M6 plan.

Original Opus remux fallback (approved; implemented in M6.5b/c for single and collection downloads): for Original audio WebM/Opus → `.opus` lossless remux, the safe original WebM may be published with an Activity warning only under all of the following. No cause is permission on its own; the service rule decides. This does not change the tag/artwork rule above. The remux applies only when the Original output is `None`, the selected `audio_only` codec is `opus` and the actually downloaded suffix is `.webm` (not the analysis `ext`); video, merged and explicit outputs do not enter this remux.

1. Eligible: the service observes `FFmpegError` with `kind == FFmpegFailureKind.FAILED` and `cause == FFmpegFailureCause.EXIT_STATUS`, and the job's cancellation state is re-checked immediately before falling back. Cancellation wins: a non-zero exit may itself result from a SIGTERM sent for cancellation, so the absence of `CancelledError` alone is not sufficient.
2. Not eligible (job/child fails): `FFmpegFailureKind.BINARY_MISSING`, `FFmpegFailureKind.TIMEOUT`, `FFmpegFailureCause.OUTPUT_GUARD`, `FFmpegFailureCause.SPAWN`, `FFmpegFailureCause.NO_OUTPUT`, `FFmpegFailureCause.OUTPUT_UNVERIFIABLE`, and `FFmpegFailureKind.FAILED` with `cause=None`.
3. The safe original is published only through the normal storage containment, conflict and catalog checks; a failure in any of them fails the job/child. Cancellation and direct security, storage and catalog errors are never swallowed. Accepted narrow exception: `EXIT_STATUS` cannot separate a disk-full or permission error inside the FFmpeg process from a media error, so such an internal FFmpeg error may fall under this warning; storage errors are not guaranteed to be separated.

An explicit target that cannot be produced fails the job/child, with no silent fallback to another format or source; a ready explicit target keeps the tag/artwork rule in the Explicit conversion paragraph above. Product-level scope (not limited to single downloads, no change-format or retry-with-different-choice UI) is in PRODUCT_SPEC §14; the single-then-collection implementation order belongs to STATE. Staging, cleanup, retry and the single/collection lifecycle are unchanged, and source protection does not imply unbounded retention or a retry cache.

H.264 profile compatibility (approved, M6.4f-f3b): normalized formats carry an additive optional `h264_profile_idc` parsed only from an exact source `avc1/avc3.PPCCLL` codec string (codec label stays `h264`; older persisted results without it read as unknown). MP4 (compatible) transcodes video only for the denylisted profiles High 10, High 4:2:2, High 4:4:4 Predictive and CAVLC 4:4:4 Intra (`profile_idc` 110/122/244/44); usual, other and missing profiles keep stream copy, and the AAC audio rule is unchanged. There is no runtime probe or interlace detection, so player compatibility is not guaranteed for sources with missing profile information; Original is unchanged.

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

The parent also owns the collection-level Audio / Video mode and output format/profile preference. There is one preference for the job, not per-item profiles in v0.1. The backend resolves it against each child's actual source through the yt-dlp/FFmpeg integration boundaries. `Original / Best Available` chooses the most suitable available/native result, allowing heterogeneous final containers/codecs. Explicit conversion affects only this job's selected children, never existing Library or historical files. Video ceilings such as `Up to 1080p` do not upscale lower-quality sources, and conversion is not quality enhancement; a child with no candidate under the ceiling falls back to its best available result (M6-Q20 supersede note below; semantics in PRODUCT_SPEC §9). M6 plan (approved; not implemented) supersedes the earlier optional-profile wording: MP3, WAV, M4A, Opus and FLAC audio and MP4 (compatible) video are required new-download outputs (§10, §14, §59 M6); the runtime above still accepts only the native profile.

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

M5-3 create/read technical contract: existing `analysis_id + variant_id` single requests remain valid. Collection requests instead carry `item_ids`, required `collection_mode` (`audio` / `video`) and optional `collection_profile`: `{kind: "original_best_available", max_height?: positive integer}`. The ceiling (a short-edge ceiling since M6-Q20 despite the field name; see the supersede note below) is video-only and a source-aware preference, never an upscaling/quality guarantee. Unknown fields, conversion names, raw formats/URLs/CLI arguments and mixed single/collection requests are rejected. Omitted/null/native-default profile persists as SQL NULL; a video ceiling persists as preference JSON. This deliberately narrow contract establishes no conversion matrix; child probing/resolution/execution remains M5-4. Owned unexpired persisted selectable IDs are validated without extraction, with distinct nonempty selections capped at 200 and raw source order retained. Parent, children and initial activity commit together, before live publication/queue notification. Collection POST/GET/list/control responses include owner-scoped children; shared SSE mapping carries parent preferences/counters but leaves unrequested children null. Single DTO counters are null, not the unmaintained migration defaults. Child paths follow the existing read-only parent path boundary; no file operations are added.

**Historical M5-3 execution boundary (superseded by production queue binding).** `claimable_queued_jobs` filters to `job_type=single` before applying its concurrency limit. Collections remain queued and cannot reach the single runner or starve single queue draining. This is an internal transition until M5-4 supplies collection execution, not supported collection downloading or a new user-visible state. Collection cancellation/retry and child lifecycle hardening remain M5-5; this task does not add child scheduling/CAS or change single cancel/retry semantics.

---

Historical M5-4A native-resolution foundation (dispatch subsequently implemented): `YtDlpAnalyzer.probe_child` revalidates the persisted backend child URL through the existing HTTP(S) boundary, uses metadata-only `--skip-download --no-playlist` without flat retrieval, and rejects collection-shaped output without recursively probing entries. Timeout and task cancellation kill/reap the subprocess. `normalize_source_formats` retains every usable stream without the UI's resolution/identity collapse; existing public normalization and flat-25 analysis are unchanged. The pure backend-only resolver returns a selected native format plus an optional audio-only companion. Video candidates must be muxed or have an actual companion; rank actual height, width, then FPS, using yt-dlp's worst-to-best source format preference order as the remaining tie-break. Audio follows that extractor preference order rather than comparing bitrate across codecs; numeric bitrate/size is not a fidelity claim. Unknown dimensions rank below known dimensions; with an explicit height ceiling, unknown height is excluded because compliance cannot be established (source `HEIGHTp` / `WIDTHxHEIGHT` labels may establish height). No candidate above the ceiling and no upscale, extraction, conversion, codec matrix, naming, or output-file changes are introduced. Missing/unusable formats and collection-shaped results use existing malformed-output errors; unavailable modes/ceilings use invalid-variant, unsupported preferences use invalid-profile. Scheduling, aggregate policy and collection execution remain a separate bounded task.

Supersede (approved user decision; implemented in M6-Q20; product semantics are authoritative in PRODUCT_SPEC §9): the collection video ceiling picker offers the fixed set 2160/1440/1080/720/480 plus default `Best Available`. Wire/persistence compatibility: `CollectionProfile` and the persisted JSON/NULL shape are unchanged and the field keeps its historical name `max_height`, but for collection video it now means a short-edge ceiling (smallest known positive normalized dimension `<= X`; a stream with no known dimension never fits). The resolver builds the same eligible pool (muxed, or video-only with an actual audio companion), prefers fitting candidates under the unchanged rank tuple, and otherwise returns exactly the uncapped `Best Available` choice, which may exceed the cap; only an empty eligible pool is invalid-variant. The UI omits `max_height` for `Best Available` and audio. The runner re-reads the persisted profile and resolves it on every attempt, including retry; no new field, migration, probe, scaling or download/FFmpeg/storage change was introduced. The unmet-ceiling invalid-variant behaviour in the historical M5-4A note above is superseded. Collection analysis needs no per-item height or extra probe to offer the picker. The smallest covering standard ceiling (608 → 720p) and `Best Available`-only without a reliable height remain single-item rules (PRODUCT_SPEC §9).

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

Retrying later may allow yt-dlp to continue from a valid `.part` file when supported. This is not a universal promise. Cancelled single Retry (Q2, implemented; smoke status: STATE.md): the single runner keeps the job staging (keyed by job id) on cancel and removes it only after a successful finish; the FFmpeg adapter still discards its half-written output and placement stays cancellation-gated, so nothing partial is published. Retry eligibility is the shared status-aware `single_job_retryable` (any cancelled single job, or a failed one with a retryable persisted code); `stage_retry_job` uses the same predicate in its CAS (single rows only), and `CANCELLED` is not added to the general retryable code list, so collection policy and the error body are unchanged. The existing order is kept: lifecycle lock → old attempt drain → CAS commit → `reset_attempt`, which also drops a stale queued-cancel signal before a new attempt can be claimed. The next attempt reuses the staging, so yt-dlp continues a valid `.part` when the source supports it, otherwise it starts from scratch; no automatic resume. A cancelled collection can be retried and resumes from retained child `.part` data (smoke PASS).

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

Post-v0.1 (future backlog; §47, §60): not part of v0.1 and not implemented. v0.1 settings come from Compose/environment; no `app_settings` migration is planned before v0.1.

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

**v0.1 alignment with PRODUCT_SPEC §16 (approved; planned, not implemented; no new rule or method is designed here).** An exactly identical request whose result already exists keeps the existing file unchanged and is shown as `Already exists`, not `Completed`. Any other name collision uses the `(2)` suffix; an existing media file is never silently overwritten; the naming stays centralized in `StorageService`. A video name carries the real resolution of the produced file (PRODUCT_SPEC §16). User-selectable Replace and Keep Both are v0.2 (M9-C, §59); the Replace readiness guarantee above (replace only after the new file is prepared, never delete first) and the open design status of those options are retained.

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
- Artist folder: explicit album artist first; otherwise the first name of the structured `artists` list; otherwise the single source text as-is. Never split on `&`, commas or `feat.`. Different explicit album-artist values inside one reliable album group give `Various Artists`; a missing album artist alone is not an inconsistency. Grouping/preparation and turning a multi-name `album_artists` list into a folder value are M6.6 detail decisions; no hypothetical rule is defined here, no Library-wide merging by album name and no later file moving.
- Missing artist or album keeps the current flat audio layout; the missing `Artist/Album` folders are not created and no placeholder (`Singles`, `Unknown Artist`) folder or tag is added. A missing track name falls back to the source title.
- Audio `NN`: a generic audio playlist follows the original source order, unselected numbers not compacted. In a reliable album group with a known track number, `1-01 - Title` when multi-disc is known, `01 - Title` when single-disc is known; unknown track numbers are never invented and multi-disc is not assumed when the disc count is unknown. Source order is never an embedded track tag. Video numbering (M6.3) is unchanged.
- No automatic title cleanup; the user may edit the name manually.
- `cover.jpg` is conditional: it appears only when an album-wide cover has been chosen for an existing album folder (§34). It is not chosen automatically when tracks have different thumbnail covers; the album-wide selection detail stays an open M6.7 design follow-up.
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

M6.3 (implemented): collection video files are named `NN - Title.ext` inside `StorageService` from the persisted child `source_order` (at least two digits, never compacted or truncated) and the persisted title; standalone video and audio filenames are unchanged. No automatic title cleanup; the title is the source title; manual name editing and audio numbering (approved rules in §31) remain planned, not implemented.

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

M6.7 source artwork decision (approved; planned, not implemented): by default a track's own thumbnail is embedded as a square crop; the playlist's general image is not used. A missing or broken cover never blocks the download. The user removes or replaces it on the M6.8 selection screen. The earlier rule requiring explicit selection for every source cover is superseded and no longer active. Album-wide `cover.jpg` is not chosen automatically from differing track thumbnails; its selection detail is an open M6.7 design follow-up. A single cover is not applied automatically to all tracks of a playlist with different albums. Format support is not guaranteed. Selecting a cover does not change other metadata by itself.

Alternative cover search (M6.9; approved; planned, not implemented): runs only when the user explicitly asks for it on the M6.8 selection screen, never automatically (the `Alternative cover search` button), with editable artist/album, an external catalog search, candidates shown with source/release information and, where verifiable, resolution; the user still chooses the external candidate. High pixel size is not proof of the correct release. No silent search, selection or replacement. A failed search or missing cover never blocks the download. This is the last separate M6 sub-task, not started; provider and rights terms must be clarified first, and it is not deferred or completed automatically. MusicBrainz + Cover Art Archive is only the first provider candidate and a single chain, not two independent catalogs; it is not a final provider decision and no integration exists; rights and usage questions are handled when M6.9 integration is reached, with no new research now. No new provider framework, plugin system or separate service is planned.

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

Scope: M8 device delivery is deferred to v0.2 (PRODUCT_SPEC §11); this section is the target design, not v0.1 work.

---

# 37. Library Model

The Library is not merely a directory listing.

It is a database-backed catalog of media Downloader knows about.

Filesystem access should go through Library/Storage services.

The frontend must never receive arbitrary delete-file endpoints that accept filesystem paths.

Deletion uses media/library IDs.

---

# 38. Delete Behaviour

UI model (approved; planned, not implemented; PRODUCT_SPEC §21, §31): the two backend primitives below stay separate, but the UI becomes one Radarr-style Delete modal with `Also delete files` OFF by default. Unchecked calls only the catalog remove (file kept); checked runs the existing safe selected-copy delete (per-copy selection once M7 has several copies). Remove is no longer unconfirmed. No arbitrary path is introduced and the shared-reference guard below is unchanged. The shared-delete error message must tell the user to remove the other catalog references first (finding 4); it is not a guard bypass.

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

Display (implemented): after a successful analysis of the current URL, the `auto` option label shows `Auto Select — <detected_platform_label>`; it is derived from the current result, not stored, and the request hint stays null.

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

**Current status (observed; user-transferred spec audit).** Settings are environment-only today, including max concurrent jobs. There is no `GET`/`PATCH /api/v1/settings` endpoint, no settings model and no `app_settings` table; the Settings page is a placeholder. The section above remains the target direction, not implemented behaviour.

**Settings plan (APPROVED user decision; planned, not implemented).** v0.1 includes only S1; settings stay Compose/environment-based with fixed defaults until after v0.1. Placement is in §59 M6-Q.

- Startup storage write probe (approved behaviour; technical boundary is the plan, no implementation yet): at application startup the backend test-writes and deletes a probe in each configured target root and the staging root. Success is silent; a failure shows a persistent UI banner. The probe runs behind `StorageService` with root containment: the probe name is backend-generated (never browser-supplied), created exclusively and without following symlinks, and only that own probe is removed. Results are kept in memory per startup; there is no migration or persisted probe state. A result describes the current process's permissions at that point in time only, not a guarantee of future writes (for example host ownership/mount changes later).
- S1 (read-only; approved, to be validated against the M7 target): `GET /api/v1/settings` showing effective environment values — display paths, max concurrency, Skip Existing conflict default, download defaults — plus configured/unconfigured destination state and the stored startup probe result shown read-only. The read `GET` never runs a new probe and mutates nothing. No arbitrary browser paths, no secrets or general config dump, no `PATCH` and no migration.
- S2a/S2b/S2c (persisted; post-v0.1 future backlog, §60): `app_settings` migration and DB-backed `GET`, validated `PATCH`, and Settings UI defaults. Not v0.1 and not scheduled in M9. Design points (DB preference versus environment bootstrap fallback; concurrency bounds and effect on current jobs) are deferred with it.
- Optional welcome screen: post-v0.1, not v0.1.

**Browser-local preferences (approved; planned, not implemented; frontend-only, no backend).**

- Remember last selection (v0.1): the frontend stores the user's last choices in browser `localStorage`; there is no backend preference storage, and S2 settings stay post-v0.1. Only choice-shaped identifiers are stored — never raw URLs or full metadata. This is distinct from the `sessionStorage` draft restore, which holds job/analysis IDs (K2; v0.2, §59). Technical boundary (proposal; exact fields and versioning are design, not a user-defined preference set): restore only values that are still among the currently available, validated choices; a corrupt or unknown entry falls back to the default; denied/unavailable storage degrades to defaults without error.
- Theme Light/Dark/System (future; not v0.1-required): may later be added as frontend-only `localStorage`. Observed today: `frontend/src/styles.css` defines `:root` tokens with a `prefers-color-scheme: dark` override (system support exists); the App has no manual selector or persistence. Expected cost is a small bounded frontend task: extend via `data-theme` with simple token mappings (no theme framework), and update `frontend/scripts/check-contrast.mjs`, which currently parses the media-query block.

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

v0.1 decision (approved; supersedes the retention-period example above; PRODUCT_SPEC §17, §59 Q3 technical contract, planned, not implemented): failed and cancelled staging is retained with no time-based or failure-time automatic deletion; it is removed by a successful Retry finish or by Clear History (failed always, cancelled only with `include_cancelled`).

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

**v0.1 active plan (user-approved order; authoritative over older order/timing wording below; each item's own text states what is implemented):**

1. Q2: explicit cancelled single Retry, partial staging retention and Queue/New Download access — one Opus task; implemented (§20); acceptance is a real Compose Cancel → refresh → Retry → continue-from-partial run (user smoke; status: STATE.md). Q1 is implemented; its batched UI smoke status: STATE.md.
2. Finding 3: real resolution / `(2)` / `Already exists`; the same request existing means skip, never a silent overwrite. Collection quality ceiling picker 2160/1440/1080/720/480 with per-item best fallback is approved and implemented as M6-Q20 (short-edge ceiling; §19 supersede note, PRODUCT_SPEC §9).
3. Q3 (finding 10), a separate task: bulk Clear History; preview shows only the job count (no space/byte estimate); partial failure = `N jobs could not be deleted` summary + Activity; cancelled excluded by default, `include_cancelled` option, failed staging included, Library untouched, no per-job DELETE.
4. M6.5 text tags.
5. M6.5 presentation UI plus basic error texts: the analysis-failure `View Activity` requirement and example button are removed (error message + Retry stay; Queue/history Activity stays). Platform icons are explicitly v0.1 here (PRODUCT_SPEC §4); source is the user-approved set of 10 Simple Icons SVGs (https://simpleicons.org) as local frontend assets; the license/trademark review is deferred by the user and not verified. The Auto Select detected-platform display (PRODUCT_SPEC §4) is implemented.
6. Remember last selection in `localStorage`.
7. M7 opening: storage writability check + S1 (S1 shows no result before the probe exists; not designed here).
8. Plex Music/Videos, Library extra copy, safe Delete modal (`Also delete files` OFF by default, M7 copy selection). Real Plex Music scan with M6.5 embedded text tags plus meaningful file names; if that is insufficient, only the smallest needed directory layout is a separate task. M6.6 is not withdrawn (v0.2).
9. One focused security round (see Release Security Hardening below).
10. Final fresh full gate and batched Compose acceptance.

**Deferred to v0.2 (approved; not implemented; PRODUCT_SPEC section in parentheses):** M6.6 album grouping (§13, §14), M6.7 artwork (§14), M6.8 metadata editing (§13, §14), M6.9 external cover search (§14), M8 device download (§11, §31, §35), M9-C Replace/Keep Both (§16), mobile polish beyond basic usability (§34), M9-L Library detail extras (§20), theme selector (§33), K2 draft restore (§6), analysis `Load 25 more`/`Show all` (§10; v0.1 keeps the first 25 and the 200 selected-item cap).

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

Historical scope note: M4 covered the backend (jobs API, concurrency, retry, restart handling) only. The Queue frontend plan mapping was skipped, so the Queue page stayed a placeholder after M4; this is not frontend-completed work. The Queue page was delivered later in M6-Q Q1 (current status: STATE.md). The gap is assigned to M6-Q below.

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

Approved decisions below are planned, not implemented, except the explicit-conversion work (M6.4b–e and the M6.4f real-binary smoke noted below), which is implemented, and the Original Opus remux (M6.5b/c), which is implemented; M6.5 text tagging is implemented for single audio downloads (M6.5-T3) and audio collection children (M6.5-T4), artwork remains planned and M6 is not fully closed (see §14, §31, §32, §34).

- source-quality audio handling: lossless WebM/Opus → `.opus` remux, AAC/M4A kept, no default MP3/AAC re-encode, no reprocessing of past Library items
- required conversion for new single and collection downloads (approved; implemented per the sub-task status below): audio Original / Best Available (default), MP3, WAV, M4A, Opus, FLAC with fixed profiles and no slider; video Original and MP4 (compatible, real H.264/AAC). One chosen output per job/collection; library conversion, file upload and per-child outputs out of scope. Failing explicit target fails the job/child; no silent original publish. Rules: source selection separate from output choice; existing FFmpeg/prepared_media/staging/storage boundary, no separate converter/runner. Sub-tasks:
  - M6.4a decision/spec (this document update)
  - M6.4b output-profile persistence/API: selection durable on the job, retry reuses it, missing/old profile reads as Original
  - M6.4c FFmpeg adapter conversion support
  - M6.4d conversion source resolution/pipeline with cancel/retry
  - M6.4e UI
  - M6.4f real-binary smoke with ffprobe codec/container verification
  M6.1 source_metadata, M6.2 path planning and M6.3 video numbering stay implemented; M6.4b–d (including the d1–d4 runner/API wiring) are implemented; M6.4e UI (output format selection on New Download) is implemented; the M6.4f real-binary adapter/planner smoke (codec/container verification, packet copy, cancellation) is implemented, but the full milestone closure gates are still pending, so M6.4f/M6.4 closure is not declared; M6 and M6.5+ are not complete.
- artist/album paths from explicit source/user metadata only; flat audio layout when missing; source title as fallback track name
- embedded metadata, written on staged files
- numbering: playlist video names (M6.3, implemented) use source order, not compacted; a generic audio playlist uses source order, and a reliable album group with known track/disc uses `1-01 - Title` (known multi-disc) or `01 - Title` (known single-disc); unknown numbers are never invented (§31; planned, not implemented)
- no automatic title cleanup; manual metadata/name review and editing
- sub-task order (planned, none implemented except the Original Opus remux and M6.5 text tags; M6.6–M6.9 are deferred to v0.2, see the active plan above): M6.5 text tags (Mutagen boundary, §14; single audio downloads and audio collection children implemented; closure awaits the user's real-use Docker Compose smoke result and full gates) → M6.6 grouping decision and paths → M6.7 source artwork (a track's own thumbnail as square crop by default, missing/broken never blocks; the old rule requiring explicit selection for every source cover is superseded) → M6.8 selection/editing (user removes/replaces cover) → M6.9 external search only when the user explicitly asks on the M6.8 screen; `cover.jpg` only conditionally with an album folder
- tag/artwork/remux failure fallback (PRODUCT_SPEC §14, ARCHITECTURE §14): under Original, an audio remux failure keeps the safe original with an Activity warning only under the bounded conditions in §14 (Original Opus remux fallback: `FFmpegFailureKind.FAILED` with `FFmpegFailureCause.EXIT_STATUS`, cancellation re-checked, normal storage/catalog checks; approved, implemented in M6.5b/c); when an explicitly selected target was already converted successfully, optional tag/artwork failure keeps that ready target; if the explicitly selected target cannot be produced, the job/child fails and no different original is completed; cancellation and direct security/storage/catalog errors are not swallowed (the narrow internal FFmpeg disk/permission exception under `EXIT_STATUS` in §14 is outside that guarantee)
- last separate sub-task (M6.9, v0.2), not started; provider/rights questions are handled when integration is reached: user-triggered alternative cover search, only on explicit request on the M6.8 screen, never automatic (MusicBrainz + Cover Art Archive as the first candidate, single chain, not a final choice)

### M6-Q — Queue frontend prerequisite, readiness and plan (documentation only)

Plan and decision record; current delivery/smoke status lives in STATE.md, task history in commit bodies.
Delivered so far: Q0 deployment fix (closed), Q19 log/detail changes, runtime 6 Deno/yt-dlp (closed), Q1 backend and frontend, Q2 cancelled single Retry (implemented; smoke status: STATE.md).
Q3 and the other tasks are open; M6-Q and the general M6 milestone are NOT closed.
Smoke evidence is user-reported; lead/worker did not run Docker.
Labels: **APPROVED** = user decision (planned, not implemented unless stated); **PROPOSED** = awaiting user approval, not started; **OBSERVED** = finding, unfixed.

**APPROVED**
- M6-Q (Queue frontend per PRODUCT_SPEC §17) is a prerequisite within the already-running M6, to be done before the M6.5 tag implementation; M6.5 tags wait. It does not turn the historical M4 backend scope into frontend-completed work.
- Queue content, Clear History (completed+failed default selection, failed staging deleted with them, cancelled kept unless `include_cancelled`, no single job delete, Library untouched, active/queued never candidates) is recorded in PRODUCT_SPEC §17. The clear endpoints do not exist; their planned contract, including Retry/cleanup races and filesystem ownership, is the Q3 technical contract below.
- Decisions recorded in PRODUCT_SPEC and planned, not implemented: single Audio/Video choice (§6), video ceilings and naming/collision rules (§9, §16), real conversion progress (§17), single Delete modal (§21; §38 here). The old job card may be cleared on a new analysis only after Queue actually delivers access to cancelled single-job Retry (finding 8); Queue restores job access from the backend.
- Lifecycle: the M5 orderly-shutdown contract (§19, §21) stays an implemented contract; container graceful stop is user-smoke verified for Q0 only (finding 18 CLOSED/PASS), which is not a milestone closure.
- Approved user decisions of 17 Nov (planned, not implemented; product wording in PRODUCT_SPEC):
  1. Cancelled single Retry with partial staging retention, continuing from where it stopped; explicit Retry only, no automatic resume. Separate bounded Opus expert task in Q2 — implemented (§20; every cancelled single job, including queued/pre-Q2 cancels, gets Retry; from scratch without a valid partial); smoke status: STATE.md. Clear History keeps cancelled single jobs by default; with `include_cancelled` their partial staging is deleted (PRODUCT_SPEC §17).
  2. Clear History also deletes failed jobs' staging; no automatic deletion at the moment of failure (PRODUCT_SPEC §17).
  3. `sessionStorage` draft restore (K2) is approved and deferred to v0.2 (not Q2, not v0.1). The technical sketch below stays PROPOSED (PRODUCT_SPEC §6).
  4. Collection standard `Up to Xp` ceiling picker (finding 20; PRODUCT_SPEC §9).
  5. Single item: smallest covering standard ceiling (608 → 720p), no upscaling, `Best Available` only without a reliable height. Collection (user decision, closed): fixed ceilings 2160/1440/1080/720/480; each child takes the best `<= X`, otherwise its best available fallback, which may exceed the cap; no upscaling; no per-item height or probe needed. The collection part is implemented in M6-Q20 as a short-edge ceiling, superseding the earlier invalid-variant behaviour (§19 supersede note).
  6. Work order (historical; superseded by the v0.1 active plan at the top of this section): 18 closure → 19 masked error detail → 6 Deno → Queue Q1/Q2 → 3 conflict → 10/Q3 cleanup → M6.5. Technical proposals inside these items (for example the 18 timeout values) are not approved by the order.
  7. Persistent milestone-closure question "is any v0.1-required spec item left unplanned?" — recorded as one closure-checklist rule in `docs/WORKFLOW.md` (Gate and STATE); no new gate, tool or config.
  8. Placeholder menu entries get a "not built yet" ("Henüz yapılmadı") label (PRODUCT_SPEC §22); no UI change today. The label requirement stays; its timing is open (no longer tied to Q1; the M6.5 presentation UI is only a suggestion).
  9. Plan approved (17 Nov, decisions round 2): the order in decision 6 stands (at that approval 18 was the next task; current continuation: STATE.md). Q1 covers historical Activity; Q3 is bulk-only. The finding 3 naming correctness (resolution suffix, `(2)`, `Already exists`) stays before Q3/M6.5 and does not move to M9.
  10. Settings: S1 read-only approved, to be validated against the M7 target; S2a/S2b/S2c are post-v0.1 (§47, §60); Compose/environment settings until then.
  11. No per-job `DELETE /jobs/{id}`; only bulk Clear History (user-settled; §7).
  12. Startup storage write probe of configured target/staging roots: success silent, failure as a persistent banner; S1 shows the same stored result read-only (§47 technical boundary; no implementation yet).
  13. Remember last selection in browser `localStorage` is v0.1 (§47); theme selector is optional future work; welcome screen is post-v0.1.
- Source of the spec-audit findings below: a user-transferred audit from a separate read-only Opus session on main `f597dd3`; the full report lives in the user's Claude project (`claude/spec-coverage-2026-11-17.md`). It was not read in this workspace and no current full audit is claimed here.

**Plan items** (approved plan per decision 9; current status: STATE.md; the approved order is retained; general M6-Q and M6 are not closed; technical values inside the remaining items still need approval where stated)
- Q0 (finding 18): bounded Uvicorn graceful-wait and a compatible Compose stop grace (applied values 5 s / 30 s per the user-approved approach; they bound waits only and are no settlement guarantee) as a separate fix, plus an explicit Compose smoke with SSE open, active single and collection drain, and recovery. Sonnet only if it stays a bounded deployment change; Opus from the start if lifecycle/concurrency is touched.
  Implemented and closed as a subtask by the user Compose smoke (not a lead/worker Docker rerun; details: STATE.md/commit history).
  Image CMD keeps `alembic upgrade head && exec uvicorn` (no ENTRYPOINT/init) and adds `--timeout-graceful-shutdown 5`; Compose `stop_grace_period: 30s`.
  App lifecycle unchanged: the 5 s bounds only the HTTP wait, not `queue_manager.stop()` drain → settlement → `engine.dispose()`;
  30 s is an outer budget, not a guarantee (overrun is force-killed and startup recovery applies).
  Regression evidence: `backend/tests/test_graceful_shutdown.py` (open SSE stream) plus Dockerfile/Compose config checks; not a M6-Q or M6 closure.
- Finding 19 (scope narrowed by user decision, M6-Q19): for now only the container log. When a single job or a collection child fails with a `YtDlpError`, the runner writes the raw `YtDlpError.detail` through the existing Python logging (stderr/container log) with a fixed format string and parameters: single `job_id`; collection parent `job_id` and child item id; the failure `kind` is always present, so a `None` detail still logs `detail=<none>` with a meaningful kind.
    Catch points: `backend/app/services/job_runner.py` and `backend/app/services/collection_child_runner.py` only; no traceback; error classification, DB, lifecycle, API, schema, UI and Activity payloads are unchanged.
    Keeping the raw extractor message in the private container log is the user's request; no masking infrastructure is added.
    Evidence: `backend/tests/test_ytdlp_failure_logging.py` (caplog through the real single and child runner catch flows, plus success paths with no failure log).
    PROPOSAL only, not approved implementation: a UI error detail with masking as a separate task next to the M6.5 text labels.
    Still open and untouched: a separate Interrupted badge and the SSE log simplification.
    Finding 6 (runtime 6, implemented; closed by user Compose smoke; no M6-Q/M6 closure): the `Dockerfile` runtime stage copies the pinned official Deno binary (`COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno`), installs `"yt-dlp[default]"` (brings `yt-dlp-ejs`) and fails the build unless `deno --version`, `yt-dlp --version` and `importlib.metadata` for `yt-dlp-ejs` succeed.
    No remote components, installer script, Node runtime layer or app CLI/config change; python3.12 base, ffmpeg apt, the graceful CMD and Compose 30 s budget are unchanged; general yt-dlp pinning is a separate follow-up.
    Evidence: `backend/tests/test_container_runtime.py` (Dockerfile text only).
    Smoke results (user Compose runs) are summarized in STATE.md; this is not a full gate and M6-Q and M6 stay open.
- Q19a (bounded small task; user Compose smoke finding): single-job log details carried carriage returns that let the terminal overwrite the line, plus repeated fragments and overlong text.
    Implemented at the central integration boundary `backend/app/integrations/ytdlp.py`: a pure helper `normalize_failure_detail` runs inside `YtDlpError`, so the detail is a readable single line before it reaches the container log, the new DB `error_detail` write and the child `JobError` detail.
    It strips ANSI escapes, splits CR/LF/CRLF and joins the remaining lines with one space, and drops only real progress remnants (numeric `PROG` lines and `[download] N%` progress lines). The actual error line (`[download] Got error`, `Unable to download`, ...) is never treated as progress. `None` stays `None` and an empty string stays empty.
    Classification still runs on the raw stderr in `map_failure`; no retry, lifecycle, subprocess, storage, API, schema or UI change, and old DB rows are not migrated. This cleanup is readability only, NOT masking of raw content; the meaningful error stays and no masking infrastructure is added.
    Evidence: `backend/tests/test_ytdlp_adapter.py` (`TestFailureDetailNormalization`) and `backend/tests/test_ytdlp_failure_logging.py` (single DB `error_detail`, child `JobError` detail and log are single-line).
    The same helper also collapses repeated logical messages to the first occurrence in first-seen order (a bare `ERROR:` token plus its message line, and repeated `ERROR: [download] ...` fragments already joined on one line, are handled; a message with or without its `ERROR:` token counts as the same message; different errors, including A/B/A sequences, are kept)
    and caps the detail after normalize/dedup at 500 Unicode characters including the ellipsis, appending `…` only when truncated.
    `None`/empty behaviour and classification on the raw stderr are unchanged, the same `YtDlpError.detail` feeds the log and the new DB write, and old DB rows are not migrated; no API, schema, UI, retry or lifecycle change.
    Evidence adds repeated-fragment, A/B/A, distinct-message, 499/500/501 boundary and single/child DB+log dedup/cap tests in the same two test files.
    Q19 is closed by the user; the network smoke result is in STATE.md.
- Q19 smoke notes (observed; recommendations only, no behaviour change requested): when the network dropped, most collection items failed immediately and Retry then resolved them. Stopping after N consecutive network errors is only a PROPOSAL; no behaviour change is wanted. A failed item showed 0% while its partial remained and Retry resumed from the partial. The unnecessary vertical spacing between failed item rows is a separate M6.5 presentation note, and the internal `network_error` code shown as text remains its own separate visual-text issue (M6.5 note).
- Collection analysis decision (USER DECISION; deferred to v0.2, NOT implemented; PRODUCT_SPEC §10): `Load 25 more` (+25) and `Show all` up to at most 200, with a short note when the source is longer;
  a waiting state while loading and existing selections preserved.
  v0.1 keeps the current behaviour: the first 25 items (`COLLECTION_ANALYSIS_WINDOW_LIMIT`, `--playlist-items 1-25`) and the unchanged 200 selected-item cap.
  No API is designed or implemented; the buttons do not exist. The earlier Q1/Q2-adjacent Opus placement is superseded by the v0.2 deferral.
- Private/Deleted video placeholders: a YouTube flat entry (exact `ie_key` `Youtube`) with no/null `availability` and exact title `[Private video]` or `[Deleted video]` is `selectable=false` with `unavailable_reason=unavailable`, even with a valid URL; explicit `availability` and other platforms are unchanged. Select All, default selection and the 200 cap already follow `selectable`.
- Q1: minimal server-backed active/queued/history list with Cancel/Retry, on-demand child/detail Activity, list refresh driven by the existing single global SSE connection (`/api/v1/events`, one shared connection updating the list from every job snapshot) with a REST/poll fallback when needed;
  no per-job SSE endpoint and no new framework.
  On-demand detail explicitly covers past completed/failed/cancelled jobs: persisted Activity is read through the existing backend, with no new SSE.
  `list_jobs` returns the newest 50, so older active/queued jobs can be hidden:
  ensure every non-terminal job is visible and terminal history is bounded with simple load-more/pagination;
  reduce collection children N+1 loading; on-demand detail API; backward compatibility is checked in the design.
- **Q1 technical contract** (DESIGN, documented 20 Nov).
  Status: Q1-be, Q1-fe-api and Q1-fe-page implemented (current status: STATE.md); Q2 implemented (§20), Q3 open; no M6-Q/M6 closure.
  Code: `api/v1/jobs.py` scope/limit/cursor, `repositories/jobs.py` batched children and scoped reads, `services/job_service.py` cursor codec; evidence `backend/tests/test_job_listing.py`.
  USER DECISION recorded here as authoritative: Queue history order is `created_at` newest → oldest.
  No migration, new SSE route/event, provider/framework or new detail/Activity endpoint.
  - Legacy `GET /api/v1/jobs` (no `scope`): unchanged response — `{"jobs": [...]}`, owner's newest 50 by `created_at DESC`, collection `items` included, no `next_cursor` key;
    only the per-collection `list_job_items` loop in `api/v1/jobs.py` becomes one batched owner-scoped child query for the page
    (same `source_order` order and identical JSON; existing equality tests stay as guards).
  - `scope=active`: every owned job with status in `queued`, `analyzing`, `downloading`, `post_processing`, `finalizing`, ordered `created_at ASC, id ASC`, no limit (bounded by the real queue; accepted under the homelab threat model).
    `scope=history`: owned `completed`, `failed`, `cancelled` only, ordered `created_at DESC, id DESC`,
    `limit` default 50, `1..100` (fetch `limit + 1` to decide `next_cursor`).
    A row with an unknown status is in neither scope.
  - Scoped envelope: `{"jobs": [...], "next_cursor": string | null}` (`active` is always `null`). Scoped jobs use the existing `JobDto` with collection `items: null`; `error.retryable` stays correct from the existing batched `_collection_retry_children` preload (single jobs from the persisted code).
  - Cursor: server-issued, opaque to clients — URL-safe base64 (no padding) of JSON `{"created_at": <UTC ISO-8601 with exactly 6 microsecond digits and +00:00>, "id": <32-char lowercase hex>}` from the last returned row;
    next page predicate `created_at < c OR (created_at = c AND id < i)` with `id` as tie-break,
    compared as UTC at microsecond precision (rows are written with UTC `now`; naive SQLite values are UTC).
    Clients never build cursors from DTO timestamps.
  - Validation → existing 422 handler shape `{"error": {"code": "invalid_request", "message": "The request payload is invalid.", "retryable": false, "analysis_id": null}}`:
    `scope` not `active`/`history` (including empty); `limit` non-integer or outside `1..100`;
    `limit` or `cursor` without `scope=history`;
    cursor that is undecodable, not that JSON object (missing/extra keys), naive/unparseable time, or bad id
    (raised as `RequestValidationError` so the body is identical).
    Other unknown query parameters stay ignored (FastAPI default).
    Detail stays `GET /jobs/{id}` (items included), Activity stays `GET /jobs/{id}/activity`; both remain owner-scoped 404.
  - Frontend live-update rules (Queue page; existing single global `subscribeToJobUpdates` / `/api/v1/events`; `onOpen`/`onError`, `snapshotTime` and the 2000 ms polling pattern are the ones verified in `NewDownloadPage.tsx`):
    (1) A job snapshot for a known ID is applied newest-wins by `snapshotTime(updated_at)`;
      equal time keeps current except same-status REST `items` enrichment;
      a REST response never overrides a newer SSE snapshot.
    (2) List placement follows the winning snapshot's status, so one ID is in at most one list;
      terminal moves active → history, Retry moves history → active.
    (3) An SSE job snapshot or `activity.created` for an unknown ID never inserts a row (SSE is not owner-scoped);
      it triggers one coalesced merge reconcile = `scope=active` + history head, merged per (1).
    (4) Two reconcile kinds.
      **Reset** runs on mount and on every `onOpen` (initial open and each reconnect; subscribe first, then REST):
      it fetches `scope=active` + history head, applies (1) per ID against local snapshots,
      then replaces history with exactly that head and its `next_cursor`
      (deeper loaded pages are dropped and reloaded only by Load more),
      so a row on a previously loaded deeper page that changed while SSE was down
      (e.g. failed → Retry in another tab → completed) cannot stay stale.
      **Merge** (unknown-ID trigger, polling, and once on `onError`) only merges active + head per (1)
      and keeps loaded pages and `next_cursor`, except the gap case in (7), which is a reset.
      In both, a locally active ID missing from both fresh responses is re-read via `GET /jobs/{id}` (404 removes it).
      Every reset increments a history generation;
      in-flight Load more and older reconcile history results from a previous generation are discarded
      (their active rows still pass only through (1)).
    (5) Polling the merge reconcile every 2000 ms runs while SSE is not open
      (before the first open or after `onError` until the next `onOpen`) regardless of list contents
      — so a job created in another tab while SSE is down and the active list is empty is still found —
      and also while SSE is open and the active list is non-empty.
    (6) Snapshots with collection `items == null` never delete loaded items.
      An open collection detail re-reads `GET /jobs/{id}` coalesced (one in flight, latest key, like `refreshCollection`);
      open Activity merges `activity.created` by `job_id`.
      Open details are keyed by job ID, not by list membership:
      every reset re-reads each open detail's `GET /jobs/{id}` and `GET /jobs/{id}/activity` (404 closes only that detail),
      so each stays correct even if its row was on a dropped deeper page.
      Several details may be open (presentation rules: PRODUCT_SPEC §17, M6.5-UI-History-Multi):
      each opening gets a new job-keyed slot identity with its own coalesced detail read and Activity generation,
      so a stale response after a close/reopen of the same ID, or of another ID, is discarded;
      the single top slot is allocated in the controller from the winning row/detail snapshot (per (1)):
      a detail that is Active by it (Active row, or rowless after an SSE/GET/Retry response, with no list membership change) takes the top
      and closes only previous Active selections; a displaced terminal fallback stays open but hidden and returns inline with its row;
      a fallback for dropped History rows (terminal winning snapshot only) is chosen only at a reset boundary (most recently opened), never per render.
      Every open detail snapshot is a newest-wins comparison base for reset, merge and Load more, never list membership.
    (7) History is rendered sorted by `created_at DESC, id DESC` with ID dedup;
      Load more appends `next_cursor` pages (one in flight; an error keeps the cursor for manual retry).
      A Load more response is applied only if the history generation is unchanged
      and its requested cursor still equals the current `next_cursor`; otherwise it is discarded.
      Merge reconciles merge the head without changing `next_cursor`,
      except when the fresh head has a `next_cursor` and its last row is not loaded (possible gap)
      — then a reset to that head is performed.
    (8) Cancel/Retry use the existing endpoints and `isActiveJob`/`canRetryJob`;
      the response is applied per (1); a failed mutation only reads `GET /jobs/{id}`, never repeats.
  - Recorded limits:
    keyset pages are not a snapshot — rows terminalized or created after a cursor was issued that sort above it
      come only via reconcile/SSE;
    because history sorts by `created_at`, an old job that finishes late stays visible where already loaded
      but after reload sits at its creation position, possibly beyond the first page.
    While SSE stays down, merge polling does not refresh rows on deeper loaded pages
      (a fast Retry→terminal there can stay stale until the next reset or reload;
      Load more only appends pages after the cursor and does not in general refresh already loaded stale deeper rows;
      a reset (reconnect/`onOpen`) or page reload updates them).
    `snapshotTime` ordering assumes a non-regressing server clock.
  - Acceptance (backend):
    legacy response unchanged and children batched (query-count test);
    old queued and active jobs behind 50+ newer terminal jobs all appear in `scope=active`, ASC with id tie-break;
    history contains only terminal statuses, default 50, limit 1/100,
      and concatenated pages equal the full ordered set with no gap/duplicate
      across equal-microsecond ties, 1 µs neighbours and an exact page-multiple boundary (`next_cursor` null);
    collection `items` null with correct `retryable` (failed/cancelled collection, single);
    every 422 case above;
    foreign-owner jobs never appear in either scope or via a cursor.
  - Acceptance (frontend):
    client URL/cursor encoding and error mapping;
    known-ID update, stale REST ignored, active/history disjointness and moves;
    unknown ID → single coalesced reconcile;
    SSE down with empty active list finds a new job by polling;
    reconnect recovers missed jobs and re-reads missing actives;
    a failed job on a loaded second history page that is Retried to completed in another tab while SSE is down
      shows completed (no stale failed/Retry row) after `onOpen` reset, with history equal to the fresh head and its cursor;
    a delayed Load more response arriving after a reset is discarded and does not change the list or `next_cursor`;
    mount/`onOpen` reset vs poll head merge (loaded pages kept);
    open detail/Activity re-read on reset;
    Load more dedup/sort/error;
    `items: null` keeps detail items;
    past-job Activity via REST;
    Cancel/Retry without auto-repeat;
    existing Queue heading/nav test kept.
  - Bounded order: **Q1-be** (`api/v1/jobs.py`, `repositories/jobs.py`, service if needed, backend tests; Opus — API contract)
    → **Q1-fe-api** (`frontend/src/api/jobs.ts` scoped client/types + tests)
    → **Q1-fe-page** (`QueuePage.tsx` and page-local helpers/tests; reusing/extracting `snapshotTime` must not change `NewDownloadPage` behaviour).
    Out of scope: Q2 cancelled single Retry/staging, K2 draft restore, Q3 Clear History, analysis 25/+25/Show all, placeholder label (decision 8), integer collection percent, per-child Cancel/Retry, SSE owner scoping, and any new product decision.
- Q2: approved cancelled single Retry with staging retention and Queue/New Download access (one Opus expert task; decision 1) — implemented (§20); smoke status: STATE.md. K2 draft restore is NOT part of Q2 (v0.2).
- Finding 3 (after Q2): wrong quality/identity/naming through a separate storage/catalog contract expert job that never blindly dedups the current logical shared identity; the Replace/Keep Both choice surface stays in M9-C (v0.2). The collection ceiling picker (decision 5) is implemented as M6-Q20 (short-edge ceiling; §19 supersede note).
- Q3 (finding 10), separate from finding 3: bulk Clear History; the backend is built from scratch. Separate Opus expert contract: preview/confirm, drain, retry/cleanup races, staging ownership, path/backend ID handling, symlink/no-follow, DB+filesystem atomicity limits, no arbitrary orphan traversal; failed staging is included (decision 2); cancelled excluded by default with an `include_cancelled` option; Library untouched. Decided (supersedes the earlier open question): the preview shows only the job count, no byte/space estimate; partial failure is reported as an `N jobs could not be deleted` summary plus Activity. No per-job job/history `DELETE` (§7, PRODUCT_SPEC §17); user-settled bulk-only.
- **Q3 technical contract** (DESIGN, documented 23 Nov; planned, NOT implemented; product decision: PRODUCT_SPEC §17 Clear History block). Implementer: Opus (destructive filesystem, ownership/concurrency, API contract).
  - **Blocking prerequisite (separate bounded task before any Q3 code):** `activity_events.id` is an SQLite `INTEGER PRIMARY KEY` without `AUTOINCREMENT` (migration 0003; no `sqlite_autoincrement`), so deleting the highest Activity rows lets new rows reuse their ids. `events.py` drops live ids `<= replay_hwm` and replays only `cursor < id`: a reused id can be silently dropped live on a connection whose mark predates the delete, and missed on replay by a client whose `Last-Event-ID` is above the new maximum. Activity ids must first become monotonic and never reused; the migration option is chosen in that task, not here. No new SSE channel or event type is designed.
  - Endpoints (names adjustable; shape fixed): `POST /api/v1/jobs/clear-history/preview` `{"include_cancelled": bool}` (default false) → `{"preview_id": str, "job_count": int}`; `POST /api/v1/jobs/clear-history/confirm` `{"preview_id": str}` → `{"deleted_count": int, "failed_count": int}` (`failed_count` > 0 drives `N jobs could not be deleted`). No client job ids, counts or paths are accepted; validation uses the existing 422 shape.
  - Candidates: the owner's jobs (single and collection parents) with status `completed`/`failed`, plus `cancelled` only with `include_cancelled`; active, queued and unknown statuses never.
  - Snapshot: the server generates an unguessable `preview_id` and keeps a process-local, short-lived, bounded in-memory entry: owner, `include_cancelled`, monotonic creation time and the exact members as `job_id → (job_type, status, retry_marker, updated_at, completed_at)`. The clock-independent attempt proof is `retry_marker` = `MAX(activity_events.id)` over this owner-checked job's `job_id` rows written only by the two successful-Retry writers — single `JobService._finalize_retry` and collection-parent `stage_collection_retry`, which both insert the fixed `Retry queued` Activity in the same transaction as their Retry CAS (Q3 makes that event a shared constant and tests that no other writer, runner, recovery or Clear path emits it). Because every successful Retry after the preview writes such a row, any post-preview Retry changes the MAX. The marker is compared by exact null-safe equality (`IS`) at preview, at every fresh read and inside the DELETE CAS. `NULL` only means no recorded marker row (including legacy jobs Retried before any row existed); there is no timestamp fallback. Valid only after the monotonic-id prerequisite; marker rows disappear only with a successful Clear of their job. `updated_at`/`completed_at` may serve as extra mismatch guards (a difference is a safe skip), never as the proof. Membership is never rebuilt from a count or a wall-clock cutoff. Proposed bounds: TTL 10 min, small global cap with oldest-first eviction. Confirm does lookup → expiry and owner check → consume (pop) as one synchronous step before its first await (single use). An unknown, expired, evicted, consumed or restart-lost `preview_id` deletes nothing and returns a distinct non-retryable error (proposed 409 `clear_preview_expired`) so the UI asks for a new preview; a foreign-owner request gets the same 409 without consuming the entry, so the real owner can still use it once. Neither case is member scope change, which is a silent per-job skip. No token table, column, migration or service.
  - Per job, sequentially in snapshot `created_at ASC, id ASC` order, inside `runtime_operation()`, holding one `lifecycle_lock(job_id)` at a time (never nested; no global lock): (1) lock; (2) stop signal set → skip this and all remaining jobs (skipped, not failed); (3) fresh owner-scoped read: missing row, other owner, non-terminal or no longer eligible status, or fingerprint/marker mismatch → silent skip without any drain (a Retried job may have a new active attempt; Clear never waits for it under the lock); (4) only a still-terminal, matching job: `wait_for_attempt(job_id, retrieve_outcome=True)` drains the old attempt's tail (publication, legacy staging removal); (5) revalidate (3) fresh before storage; (6) storage step; (7) DB step; (8) release. Steps (2)–(7) run as one `own_operation`; the caller re-awaits it under shield on every (also repeated) request cancellation, like Retry/Cancel, and releases the lock only after that operation returned a definite outcome (DB transaction finished, session closed). Same-job Retry/Cancel serialize on the lock: Retry first → Clear skips at (3); Clear first → Retry/Cancel then proceed and get the existing not-found result. Orderly shutdown: `stop()` drains registered operations, so a started per-job body finishes; no further job starts after (2). No SQL writer is held across storage work.
  - Identity: only DB-derived `download_jobs.id` (single staging key) and the parent's `job_items.id` values (collection child keys) read fresh under the lock. Never `staging_path`/`final_path`, client paths or directory scans. The existing `remove_staging` (path `resolve`, `rmtree(ignore_errors=True)`) is forbidden for Q3.
  - Storage boundary: the authoritative threat model is the PRODUCT_SPEC §17 Clear History storage threat model. The current startup capture (`initialize_delete_roots`) covers only Library/Plex roots, not staging. Q3 adds a startup staging-root identity capture with the same pattern (`_capture_root_descriptor` lexical steps + `(st_dev, st_ino)`; staging is mandatory, failure fails startup like App Library) and a Q3-only removal that re-walks and re-checks that captured identity per operation, fail-closed. Before the first deletion, the application checks the configured staging and Library/Plex roots and their aliases, and refuses to delete anything if it detects equal or nested staging/media roots or a Library/Plex access chain through staging. Host backing relationships not detectable by these checks are subject to the Deployment boundary below. The target is derived only from a DB-sourced job/child id (`download_jobs.id` or `job_items.id`) validated as one path segment, and deletion is limited to that job's staging tree. An invalid job key is rejected without any filesystem access; a mismatch with the staging root captured at startup is rejected by a filesystem check before the first deletion. The key is validated as one path segment (existing `_validated_job_id`). The validated job tree is removed with `shutil.rmtree`; on the target Linux container environment `shutil.rmtree.avoids_symlink_attacks` is `True`, so the application writes no additional fd-based deletion code. This does not remove the existing fd walk used for the startup root snapshot verification. The staging root's own mount (for example the normal Compose bind of the staging root) is the valid reference. Every descendant — directory or file — must stay on the root's `st_dev` and mount identity; any sub-mount (different device, a same-`st_dev` bind mount, a file bind mount) refuses that job's removal (storage error) and is never deleted through. If the platform cannot provide a mount identity, only the Q3 removal fails closed; the safe startup root capture and all other paths are unaffected. Symlinks inside staging are never followed; symlinks and files are unlinked as names, so link targets, other staging keys and Library files are never followed or removed (a hard link to a Library file only drops its staging name). A missing key is success (idempotent); every other error is raised, never ignored. No byte accounting. External filesystem or mount changes during deletion are out of scope (PRODUCT_SPEC §17); review findings that rest only on that assumption are not blockers. Storage configuration (roots, aliases, mounts) is changed only with the application stopped. In-app Retry/Cancel coordination (lifecycle lock, per-job steps above) is unchanged. Legacy `remove_staging` callers (runner success paths) are unchanged.
  - **Deployment boundary (approved; PRODUCT_SPEC §17 Clear History storage threat model):** supported Clear History deployments follow the PRODUCT_SPEC §17 rule: the staging host source is distinct from, and not nested in either direction with, each Library/Plex bind source, on case-sensitive host filesystems (not a rule about media sources relative to one another). The application rejects the overlaps it detects before deletion (the root and alias checks in the Storage boundary bullet above); it does not guarantee that every host backing relationship is detectable. Undetectable host relationships, including separate bind views whose backing path components differ only by letter case, are an operator deployment precondition (PRODUCT_SPEC §17), not something the application validates. No claim is made that such a configuration was verified safe. The nofollow, per-job-only deletion, DB-derived ID, static mount and root verification requirements above are unchanged.
  - Order and atomicity limits: storage first, then one short per-job transaction: conditional `DELETE download_jobs` matching id, owner, status and fingerprint, with the marker re-checked by null-safe equality in the same statement (an expected-absent marker that now exists, or any different MAX, matches nothing). Only `rowcount == 1` continues in the same transaction with `DELETE activity_events WHERE job_id = id` (child Activity is stored under the parent id; FK cascade removes `job_items`) and commit. `rowcount == 0` → rollback with no side mutation, silent skip (the job's Activity stays). Any other rowcount → rollback, fail closed, counted failed. An error in the Activity DELETE or before COMMIT rolls back, so job, children and Activity stay together; staging already removed is not restored. If COMMIT reports an error (it may still have persisted), the session is closed and a fresh owner/job read decides: row gone → counted deleted; row present → counted failed; read impossible → class (c), stop, with no guessed `failed_count` and no Activity. Narrow exception to this fresh-read rule: a COMMIT error classified as a systemic SQLite failure (e.g. `SQLITE_IOERR`) is class (c) without a fresh read: masked 500, unknown outcome, no guessed counts or Activity warning, no subsequent jobs touched; staging for the current job was already removed and is not restored; if the job remains and is retryable, Retry may start from scratch; when the database recovers, refresh and obtain a new Clear History preview. This is an accepted limitation; other ambiguous per-job COMMIT outcomes still use the fresh read. `library_items`/`media_files` are untouched (`library_items.job_id` has no FK and remains a historical reference). There is no FS+DB atomicity: after a DB failure or crash following storage removal the job stays visible with its staging gone; a later Clear or Retry handles it (Retry starts from scratch).
  - Failure classes: (a) per-job storage error → job record kept (staging possibly partly removed), one Activity warning on that job with fixed text (no path, no raw exception; raw class/errno only in the container log with the job id), counted in `failed_count`, continue with the next job; this warning is the approved error detail and is written only while the database is writable, best effort (if it cannot be written, the job still counts as failed). (b) per-job DB error → after rollback a fresh session is opened; only if the database is usable there and the job's outcome is definite (see the order bullet) is the job counted, a best-effort warning written in a separate transaction, and processing continued. (c) systemic DB failure or an unverifiable outcome (snapshot read, fresh session or read impossible) → no further job is started; there is no existing Jobs DB error mapping to reuse, so Q3 defines one explicit generic masked error (fixed message, no raw text), not an `N jobs could not be deleted` summary; no per-job Activity or guessed count is promised, per-job deletions already committed stay and become visible on the UI reset, and a new preview is required.
  - Live updates: no new SSE event; the requesting Queue page runs its existing reset reconcile (Q1 rule 4) after every confirm response, success or failure, so partial commits become visible; other tabs drop deleted rows on their next reset, and an open detail of a deleted job gets 404 and closes.
  - Impact analysis (paths that read/write the touched rows; keep behaviour, add targeted tests): `download_jobs` status/attempt reads — JobService get/list/history scopes, job DTO and SSE snapshots, Activity API owner/job existence, ORM implicit row loads, queued claim, Retry CAS, Cancel, runner completion/failure and startup/shutdown recovery; `job_items` claim/child completion/cancel/retry/detail/list and FK cascade on job DELETE; status guards with no mutation on races and correct post-delete list/detail (404, absent from scopes); Activity writers — both Retry writers (marker) and all other service/runner/recovery inserts (never marker) — list/latest/per-job detail, `max_event_id`/replay/live and the prerequisite's migration tables/indexes, marker owner/job restriction and unchanged read order plus SSE regressions; `library_items`/`media_files` provenance, list/detail/delete with rows and files untouched and independent of job rows; storage root initialize at startup (staging capture added, existing roots unchanged), single/child staging producers, legacy cleanup and Library delete roots unchanged, Q3-only removal path separate; Queue page reset on both success and error confirm responses. Guards per path: marker null-safe equality at preview/fresh read/CAS, owner on every read, rowcount rules, no drain or mutation on a lost race. No new column, table, service or event channel; this document changes no code.
  - Required tests: exact snapshot (a different job with an equal count, or one terminalized/created after preview, is never deleted); Retry→same terminal status between preview and confirm skipped by the marker with the clock frozen or set back; legacy `NULL` marker → post-preview Retry → skipped; expected-absent marker that appears before the DELETE → CAS `rowcount` 0 skip; foreign, active, queued and unknown-status jobs untouched; a foreign-owner confirm gets 409 without consuming the entry and the real owner then uses it exactly once; cancelled only with `include_cancelled`; collection child staging removed by `job_items.id`, completed child outputs untouched; storage: invalid job key rejected without any filesystem access; staging root that no longer matches the startup capture rejected by a filesystem check before the first deletion; staging root changed after startup but before the deletion starts → nothing removed; wrong key/path (not a single segment, traversal or absolute) rejected before any filesystem call, and removal never reaches another job's staging tree; same or nested staging and Library/Plex roots, including via aliases, refused before the first deletion with nothing removed; a Library/Plex media access chain that passes through staging refused before the first deletion with nothing removed; nested symlinks to outside the root, another staging key and Library keep their targets; different-device, same-device bind and file bind sub-mounts refused while the root's own (Compose bind) mount is accepted (simulated via stubbed stat/mount identity, no privileged fixture); mount identity unavailable → host startup succeeds, Q3 removal refused; Library hard link and catalog rows survive; missing staging idempotent; storage error → job kept, generic Activity, no path/exception text; DB failure after storage → job visible, staging gone, counted; `rowcount` 0 → no mutation, Activity kept; unexpected rowcount, Activity DELETE failure and pre-COMMIT failure → job, children and Activity intact; COMMIT error with persisted delete → fresh read counts it deleted; COMMIT error with unverifiable outcome → generic masked error, no further job, no guessed count/Activity; usable DB with definite per-job failure → continue to the next job; partial result with Activity only on remaining jobs; deleted job's `job_items` and Activity gone; deterministic interleavings on the same lifecycle lock: Retry-first skip without drain, Clear-first then Cancel/Retry not-found, a Cancel waiting on the lock progressing only after the full per-job outcome, two/repeated request cancellations not releasing the lock early, shutdown before/during a per-job body; expired/consumed/restart-lost snapshot no-op; generic masked error on systemic DB failure; SSE id reuse/replay regression after deleting the highest Activity ids (with the prerequisite).
- Technical sketch for K2 draft restore (v0.2; PROPOSED, unapproved; not a Q2 item): reuse the existing `GET /api/v1/analysis/{analysis_id}` and its 410 for expired snapshots; keep only `analysis_id` plus bounded selection/output/mode/platform identifiers in `sessionStorage`; no raw metadata or full source URL in persistent `localStorage`. An expired, foreign or missing snapshot, a double submit after create, or a stale variant clears the draft safely with a message. Queue alone does not fully solve New Download state loss.
- Technical record for the Queue decisions (observed code boundaries, not decisions): `QueuePage.tsx` was an 8-line placeholder before Q1-fe-page (now the live server-backed Queue page, 7fed194); M4 closed only the backend. Historical (pre-Q2): single cancelled Retry was rejected (`JobService._retry_job` accepted only a failed single job with a retryable code) and the single runner removed staging on cancel; superseded by Q2 — current single behaviour is in §20. A collection cancelled parent can be Retried and resumes from retained child staging (unchanged).
- Settings S1 (read-only; approved; contract in §47): position after M6.5, at the opening of M7, validated against the M7 target. It shows real effective env values, configured/unconfigured destinations and the stored startup probe result right when M7 adds Plex destinations. Implementer: Opus, because the API contract matters (size is not a criterion). The startup storage write probe (§47) must exist for S1 to display its result; S1 shows no result before the probe exists, and the probe is built at the M7 opening (active plan item 7), not designed here. It also relates to the root-owned container data note (finding 18 close, item 6): a non-root UID/GID plan is a separate bounded Opus task.
- Settings S2a/S2b/S2c: post-v0.1 future backlog (§47, §60); not in M9.
- Remember last selection (§47; v0.1): a separate bounded frontend task (active plan item 6), independent of the K2 draft restore (v0.2); it stays v0.1.
- Theme Light/Dark/System (§47): v0.2; no M9-T slot in v0.1.
- Spec-audit gaps (assignment only): the `View Activity` requirement and example button on the analysis-failure message are removed (approved; PRODUCT_SPEC §5; UI change in active plan item 5; error message + Retry stay, Queue/history Activity stays). Library detail thumbnail/extractor/membership gap → M9-L (v0.2), depends on M6.6 grouping (v0.2). Concurrency is env-only and tied to Settings (S1 display; changing it through S2 is post-v0.1). Replace / Keep Both behaviours and choice surface only → M9-C (v0.2); the approved resolution suffix, `(2)` and `Already exists` correctness stays in the finding 3 expert task (after Q1/Q2, before Q3 and M6.5), which M9-C preserves and aligns with — no joint delivery and no M9 dependency for that pre-M6.5 work; no conflict primitive is implemented today. Plex/Device shown honestly as coming later is fine: Plex is planned in M7 (v0.1), Device in M8 (v0.2).
- M9 scheduling (superseded by the active plan): the approved UI behaviours (video ceiling/naming display, conversion progress, platform display/icons; icons timing: plan item 5) stay approved; the Library single Delete modal is in M7 (plan item 8). Wrong-quality (3) and naming correctness are not M9 items: they follow Q2 as a separate expert job (plan item 2). The icon set and license are chosen later after approval/research; no dependency is added now.
- Closing: complete plan, milestone-close independent review, full gates, the closure question (decision 7), and a user Docker Compose smoke of 3–5 items.
- Post-v0.1 suggestions (clip ranges, subtitles) live in PRODUCT_SPEC §33 and are not v0.1.

**OBSERVED — unfixed** (readiness; not decisions)

| Ref | Finding |
| --- | --- |
| K1 | Queue page was a placeholder with no job access; implemented in Q1-fe-page (current status: STATE.md) |
| K2 | New Download state is lost on navigation/F5; Queue restores job access but does not fully solve it; draft restore deferred to v0.2; single vs collection cancel differs (PRODUCT_SPEC §17) |
| K3 | User-transferred spec audit received (source above; not re-verified in this workspace); findings assigned in Plan items; closure question approved (decision 7) |
| S0 | Settings page is an 8-line placeholder; env-only config, no settings endpoint/model/table; PRODUCT_SPEC §23 scope reduced to one word in M9 (audit) |
| L1 | Library detail lacks thumbnail, extractor/source and playlist/album membership (audit; PRODUCT_SPEC §20) |
| 3 | Wrong-quality skip plus duplicate risk (shared-file logical rows are deliberate under skip-existing) |
| 4 | Shared delete UX: message must explain removing catalog references first |
| 6 | Historical: no Deno at observation time; runtime6 now CLOSED/PASS (see Plan items). Transient `network_error`→retry success was observed; causal link to Deno remains unproven. |
| 9 | Raw float progress shown (collection `CollectionProgress` percent; integer display planned in Q1/Q2, see finding 18 close note 1); cancelled duplicate with doubled Activity (Cancelled plus a separate entry); separate A/V 50→100 jump |
| 10 | Cancelled 102 MB and restart 327 MB partials (success/retry cleanup exists) |
| 11 | After retry, child downloading/converting while parent Analyzing twice |
| 12 | Audio sequence planned for M6.6; video sequence implemented; not a regression |
| 18 | See below |
| 19 | Real extractor detail is in the DB. The container-log gap is fixed (Q19, see Plan items); the UI error detail is still missing and stays an open follow-up (masking proposal next to the M6.5 text labels) |
| 20 | No collection resolution UI; 4K AV1 took 5+ minutes, child about 11 cores / 3.2 GB RAM (user measurement) |
| 7 | Conversion UI at 100% for minutes while ffmpeg used about 1000% CPU (maps to the approved progress decision) |

Finding 18 (observed; closed as a subtask by the user Compose smoke, results in STATE.md):
graceful stop with an open SSE stream waited unbounded; this was reproduced locally and bounded by the Q0 fix.
The earlier force-kill observations are historical and SUPERSEDED by that smoke.

Finding 18 close (user report; lead/worker did not rerun Docker or ffmpeg; Python 3.12 is the earlier user setup and was not re-proven by that smoke):
- Restart recovery with Retry and partial resume is demonstrated for a single job and a collection:
  completed children are kept, the interrupted child ends `interrupted_by_restart`, a queued sibling does not auto-start (no auto-resume),
  and Retry uses the persisted first-analysis items without re-downloading completed children.
- It is NOT a single-job user Cancel retry (at that time the cancelled path rejected Retry and removed staging; superseded by Q2, see §20);
  resume is source-supported, not a universal promise.
- The `NewDownloadPage` subscription guard is `jobId == null`, not `jobActive`, so a tracked terminal job can stay subscribed;
  an idle shutdown duration alone does not prove an actual browser connection, and no forced 5 s is expected.

Non-blocking findings recorded at 18 close (open; no implementation, no fix claimed):
1. `CollectionProgress` shows the raw long float percent; show an integer. Small bounded fix planned in Q1/Q2 collection progress UI (updates finding 9).
2. The internal `interrupted_by_restart` prefix appears on the child line; replace with a human-readable message / error reason (19 and M6.5 polish, user requested).
3. A queued child under an interrupted parent needs a truthful label (M6.5 UI); no DB queued enum or auto-run policy change.
4. A separate Interrupted badge (19): PRESENTATION ONLY evaluation, decision pending; no new backend status enum is approved.
5. Expected SSE cancel at shutdown prints a huge traceback (19 diagnostics): evaluate a narrow known-shutdown-cancellation concise one-line log; real exceptions must not be hidden, no global filter or new runner, and no fix claim without evidence.
6. Container data files are root-owned (user report, corroborated: Dockerfile has no `USER`). This is known deployment technical debt, not an approved long-term design. Link to the M7/S1 writable-check (§47 startup probe) as a separate bounded Opus task: a non-root UID/GID plan compatible with existing files; no chown/migration implementation and no invented user values.

Coverage closure question: full v0.1 coverage was not re-audited; previously recorded audit gaps remain assigned, no new scope is claimed, and this bounded record leaves no newly orphaned gap.

User smoke limits (user reports; lead did not rerun; passed results are in STATE.md/commit history):
- Original 4K AV1 output is `.mkv` as expected.
- A Mix ≥25 analysis pass is not evidence for extraction of all >25 items.
- Not yet tried: collection remux and explicit-conversion Cancel.

## Milestone 7 — Plex Destinations

- Plex Music
- Plex Videos
- staging → final transfer
- destination validation
- Keep a Copy in App Library (an actual extra copy), safe single Delete modal with per-copy choice (`Also delete files` OFF by default; PRODUCT_SPEC §21)
- opening: startup writability check + read-only S1 (§47)
- Plex Music verified by a real scan with M6.5 embedded text tags plus meaningful file names; if that is insufficient, only the smallest needed directory layout is a separate task. M6.6 is not withdrawn (v0.2).

## Milestone 8 — Device Download (v0.2; not v0.1, not implemented)

- browser file delivery
- large-file behaviour
- download filename handling

## Milestone 9 — Polish (v0.1 keeps only the basics; the rest is v0.2)

- responsive mobile UI (basic usability v0.1; further polish v0.2)
- error messages (basic texts in v0.1, plan item 5)
- Settings (PRODUCT_SPEC §23; read-only S1 approved at M7 opening, §47 and §59 M6-Q; persisted S2 is post-v0.1)
- conflict policies (Replace/Keep Both: M9-C, v0.2)
- end-to-end smoke tests
- documentation

M9 sub-tasks (all v0.2; not started):

- M9-C conflict/storage semantics: only the Replace and Keep Both behaviours and their choice surface, as a separate design; Opus (destructive filesystem). The approved resolution suffix, `(2)` and `Already exists` correctness (PRODUCT_SPEC §16) is not M9-C scope: it stays in the finding 3 expert task after Queue Q1/Q2 and before Q3 and M6.5; M9-C preserves and aligns with that contract, with no joint delivery and no M9 dependency for the pre-M6.5 work.
- M9-L: Library detail gap (thumbnail, extractor/source, playlist/album membership; PRODUCT_SPEC §20); depends on M6.6 grouping.
- M9-T: Light/Dark/System theme selector (§47), v0.2.
- Settings S2a/S2b/S2c are not M9 work; they moved to post-v0.1 future backlog (§47, §60). Existing responsive/error/e2e/docs polish is kept.

## v0.1 Release Security Hardening / Audit

One focused release security round (active plan item 9): one existing reviewer's read-only pass, not nine separate tasks, after the v0.1 feature items 1–8 and before the final gate (item 10). Status: NOT STARTED / planned. Documenting this stage does not authorize starting it, is not execution or acceptance, and does not change the current milestone plan or the next bounded task.

This stage is not a new feature, not post-v0.1 scope, not a public-exposure or authentication change, and not continuous security infrastructure. The current private Tailscale / no-auth assumption (PRODUCT_SPEC section 24) and the security rules of sections 49–50 remain unchanged. No recurring telemetry, dashboards, metrics, or recurring audit infrastructure is introduced.

Audit execution model:

- The initial audit is read-only and performed by an existing reviewer role (see `docs/WORKFLOW.md`); no new agent/role is added and the auditor applies no fixes directly.
- Every finding includes evidence, affected boundary/files, realistic preconditions and impact, severity rationale, and a proposed mitigation.
- An auditor severity label alone is not acceptance: a second model (different family) verifies only the meaningful findings, with evidence, before any implementation.
- False positives are explicitly dismissed with evidence, never silently dropped.
- A verified Critical/Important finding is a v0.1 release blocker; each is fixed in a separate scoped task under the normal routing and cross-family review of `docs/WORKFLOW.md`, then the final normal gate runs.
- Finding dispositions are recorded. This is not a continuous or per-task audit.
- Full normal gates are not run when the audit starts; the fresh configured full gates (section 55) run once at the final release gate. Use the existing configured quality gates; do not invent new validators, new infrastructure, exploit work or operational security tests.

v0.1 checks (one pass over these areas, not separate tasks):

1. URL/redirects, source/ID handling, raw path and argument limits (section 50).
2. Subprocess without a shell: arguments, cancellation/shutdown, staging.
3. Containment, symlink/hardlink, conflicts, safe delete and publishing (section 51).
4. Retry/Cancel/cleanup, restart, double publishing.
5. Real private-network access and the existing object boundaries (owner scoping; Tailscale/no-auth assumption unchanged).
6. Server-side validation; SSE and error responses leaking sensitive data.
7. Real Compose: ports, mounts, secrets, container user, known serious dependency risks.
8. Collection/queue limits, timeouts, safe failure on a full disk.
9. Targeted regression, fresh full gate and real Compose acceptance (item 10).

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

## Persisted Settings (post-v0.1)

Former M9-S2a/S2b/S2c, moved out of v0.1 (§47): S2a `app_settings` migration plus DB-backed `GET /api/v1/settings` (migration number not fixed); S2b validated `PATCH /api/v1/settings` with safe concurrency changes (expert work; DB preference versus environment bootstrap fallback and concurrency bounds/effect on current jobs are design points for then); S2c Settings UI defaults only for conflict policies and destinations that are actually implemented. An optional welcome screen is also post-v0.1.

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
