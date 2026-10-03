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

Possible conversion variant:

```text
variant_id: "audio-mp3-128"
label: "MP3 · 128 kbps"
kind: "audio"
conversion: "mp3"
target_bitrate: 128
```

The mapping between `variant_id` and actual yt-dlp/FFmpeg execution parameters must exist only on the backend.

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

Each child item tracks its own:

- source ID,
- title,
- track/index,
- state,
- progress,
- output file.

A failed child should not necessarily invalidate every previously completed child.

Retry behaviour should be able to target incomplete/failed work.

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

For v0.1, mark them as failed/interrupted with a retryable error such as:

```text
interrupted_by_restart
```

The user can retry them.

This is safer than attempting complicated automatic recovery during post-processing.

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

Album artwork follows the same confidence treatment: medium- or low-confidence automatic artwork matches should be presented as candidates for user review and selection before embedding.

An ambiguous artwork candidate must not be silently embedded as if authoritative; explicit user selection makes it user-confirmed artwork.

This is especially important for Plex Music destinations.

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

- parent job
- child items
- item selection
- aggregate progress
- track ordering

## Milestone 6 — Metadata and Naming

- artist/album paths
- embedded metadata
- artwork
- artwork candidate review/selection for medium/low-confidence matches
- metadata review/editing
- playlist video naming

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
