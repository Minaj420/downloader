# Downloader

Self-hosted media downloader built as a graphical interface around `yt-dlp` and `FFmpeg`.

**Status:** active development — Milestones 0–5 are complete (foundation, URL analysis, first download, Library, queue hardening, playlist/album). Next up: Milestone 6, metadata and naming. See `PRODUCT_SPEC.md` for the product vision and `ARCHITECTURE.md` §59 for the milestone plan.

## Stack

| Area     | Technology                                                                 |
| -------- | -------------------------------------------------------------------------- |
| Backend  | Python 3.12+, FastAPI, SQLAlchemy 2 (async, `aiosqlite`), SQLite (WAL), Alembic |
| Frontend | React 19, TypeScript, Vite, React Router, TanStack Query                    |
| Quality  | Ruff + mypy + pytest (backend), ESLint + Vitest + Testing Library (frontend) |
| Runtime  | Single Docker container; FastAPI serves the API and the built frontend      |

## Repository layout

```text
downloader/
├── PRODUCT_SPEC.md        # product source of truth
├── ARCHITECTURE.md        # technical architecture
├── AGENTS.md              # working rules for AI coding agents
├── Dockerfile             # multi-stage build (frontend + backend + yt-dlp + ffmpeg)
├── compose.yml            # single-container deployment with persistent volumes
├── backend/
│   ├── pyproject.toml     # dependencies and tool configuration (ruff, mypy, pytest)
│   ├── alembic.ini
│   ├── migrations/        # Alembic migrations
│   ├── app/
│   │   ├── main.py        # FastAPI application factory + SPA serving
│   │   ├── config.py      # environment-based settings (DOWNLOADER_* variables)
│   │   ├── database.py    # async engine, session factory, SQLite pragmas
│   │   ├── api/v1/        # versioned API routes (/api/v1/...)
│   │   ├── services/      # queue manager, job runners, storage, analysis, library
│   │   └── integrations/  # yt-dlp and FFmpeg subprocess adapters
│   └── tests/
└── frontend/
    ├── src/
    │   ├── app/           # application shell (routing, providers)
    │   ├── api/           # API client and hooks (REST + SSE)
    │   ├── components/
    │   ├── features/      # new-download and library feature components
    │   └── pages/         # New Download / Queue / Library / Settings
    └── tests/
```

## Development setup

### Backend

Requires Python 3.12+.

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

- Health endpoint: <http://127.0.0.1:8000/api/v1/health>
- Interactive API docs: <http://127.0.0.1:8000/docs>
- Local data directories default to `backend/data/` (git-ignored).

### Frontend

Requires Node 22.22.2 or newer (Node 24 LTS recommended). The minimum is driven by the
locked test tooling (`jsdom`/`vitest` engine requirements) and is enforced via
`engines.node` in `frontend/package.json`.

```bash
cd frontend
npm install
npm run dev
```

- Development UI: <http://127.0.0.1:5173>
- `/api` requests are proxied to the backend at `http://127.0.0.1:8000`.

## Checks

Backend (from `backend/`, with the virtualenv active):

```bash
ruff check .
mypy app tests
pytest
```

Frontend (from `frontend/`):

```bash
npm run lint
npm run typecheck
npm test
npm run build
npm run check:contrast
```

### Manual smoke checklist (real binaries and network)

The automated checks above run against fake binaries and fixtures; they do not
prove real yt-dlp/FFmpeg/network behaviour. This checklist is manual and
optional, has not been executed as of writing, and passing it is not a release
acceptance gate. Use only small, permitted, non-DRM public samples you are
allowed to download. For the development setup above, set
`DOWNLOADER_STATE_DIR` / `DOWNLOADER_STAGING_DIR` / `DOWNLOADER_LIBRARY_DIR`
to separate, empty throwaway directories before migrations or app startup.
For Docker, host `DOWNLOADER_*` directory variables do not change the fixed
`./data/state`, `./data/staging`, and `./data/library` bind sources in
`compose.yml`. Use a separately configured smoke deployment whose effective
three host bind sources are separate, empty throwaway directories mounted at
`/data/state`, `/data/staging`, and `/data/library`; verify those actual sources
before starting it. Do not run this checklist against existing data mounts;
if isolation is not verified, mark it blocked. Keep a short private record per
run (date, environment, yt-dlp/FFmpeg versions, source URLs, job IDs,
PASS/FAIL/blocked per item) without ever including credentials or tokens in it.

1. **Runtime binaries** — in the runtime you actually use (dev venv or the
   Docker image), run `yt-dlp --version` and `ffmpeg -version`; both must
   resolve to the real binaries the pipeline calls. Record the versions.
2. **Single audio happy path** — analyze a small public audio URL, start an
   audio download, watch live progress finish, and confirm the file appears in
   the Library and plays.
3. **Video that really needs FFmpeg** — pick a video whose selected quality is
   delivered as separate video-only and audio streams, download it in video
   mode, and confirm the app performs the FFmpeg stream-copy merge and the
   output plays with both audio and video. (A source that only offers a muxed
   stream exercises no merge and is not FFmpeg proof.)
4. **Small collection** — analyze a small playlist/album, select 2–3 items and
   exclude at least 1, run it with one collection-level mode/profile resolved
   natively per child; verify children run one at a time in source order,
   per-child results and truthful completed/failed/total counters, and that
   completed outputs show up in the Library.
5. **Parent Cancel + Retry** — with one child already completed, cancel the
   parent while another child is active: running processes stop, the next child
   never starts, and the completed child's record and file stay intact. Retry
   the cancelled parent and verify cancelled or otherwise incomplete children
   are rerun, while the completed child record and file remain intact and are
   never downloaded again. If the run already contains a genuinely failed child,
   determine its eligibility from its persisted error code and verify that an
   ineligible failed child retains its state and reason. If no failed child is
   present, record the failed-child eligibility branch as **not exercised**;
   do not manufacture additional failures solely for this smoke checklist.
6. **Orderly stop/restart** — use a collection with no Cancel request: verify
   one child is completed, the parent and another child are actually active,
   and at least one sibling is still queued, then orderly stop the app (not
   force-kill), allow shutdown to finish, and restart. The interrupted active
   child and parent settle as failed with retryable `interrupted_by_restart`;
   completed records/files survive and queued siblings remain unchanged.
   This interrupted collection continues only through explicit parent Retry,
   with no auto-resume; normal unclaimed queued jobs may continue normally.
   If the active-work precondition was missed, mark this item blocked.
7. Optional, if convenient: an analysis-stage extractor error for a small
   invalid or private URL; confirm the analysis error is inspectable. This is
   not evidence of a child runtime failure. Keep this small; no extra matrices.

## Docker deployment

```bash
docker compose up --build
```

- Application: <http://localhost:8080>
- Health: <http://localhost:8080/api/v1/health>
- Persistent data is stored under `./data/` (`state/`, `staging/`, `library/`).
- The container runs `alembic upgrade head` on startup, then serves the API and the built frontend on port 8080.
- The image includes the `yt-dlp` and `ffmpeg` binaries used by the download pipeline.
- Optional Plex mounts and destinations are shown as commented examples in `compose.yml`.

### Network binding

By default, compose publishes the application only on the host's loopback interface
(`127.0.0.1`), so nothing is exposed to the network. This is the intended safe default
for a private, unauthenticated deployment (`PRODUCT_SPEC.md` §2.4, §24).

To reach Downloader from other devices in your homelab:

- over **Tailscale**, bind to the host's Tailscale IP (typically `100.x.y.z`):

  ```bash
  DOWNLOADER_BIND_ADDRESS=100.x.y.z docker compose up -d
  ```

- or, on a trusted private network only, bind to all interfaces:

  ```bash
  DOWNLOADER_BIND_ADDRESS=0.0.0.0 docker compose up -d
  ```

You can also set `DOWNLOADER_BIND_ADDRESS` in a `.env` file next to `compose.yml`,
optionally together with `DOWNLOADER_PORT` (the host-side port; the container always
listens on 8080 internally). Do not bind to a public interface without adding
authentication and security design first.

## Configuration

All settings are environment variables with the `DOWNLOADER_` prefix:

| Variable                              | Default        | Purpose                                  |
| ------------------------------------- | -------------- | ---------------------------------------- |
| `DOWNLOADER_STATE_DIR`                | `data/state`   | SQLite database location                 |
| `DOWNLOADER_STAGING_DIR`              | `data/staging` | Incomplete/temporary download workspace  |
| `DOWNLOADER_LIBRARY_DIR`              | `data/library` | App Library media root                   |
| `DOWNLOADER_PLEX_MUSIC_DIR`           | —              | Plex Music mount (optional)              |
| `DOWNLOADER_PLEX_VIDEO_DIR`           | —              | Plex Videos mount (optional)             |
| `DOWNLOADER_PLEX_MUSIC_DISPLAY_PATH`  | —              | Human-readable path shown in the UI      |
| `DOWNLOADER_PLEX_VIDEO_DISPLAY_PATH`  | —              | Human-readable path shown in the UI      |
| `DOWNLOADER_FRONTEND_DIST_DIR`        | —              | Built frontend served by FastAPI         |
| `DOWNLOADER_YT_DLP_BINARY`            | `yt-dlp`       | yt-dlp binary or path                    |
| `DOWNLOADER_FFMPEG_BINARY`            | `ffmpeg`       | FFmpeg binary or path                    |
| `DOWNLOADER_DOWNLOAD_CONCURRENCY`     | `2`            | Maximum concurrent jobs                  |
| `DOWNLOADER_ANALYSIS_TIMEOUT_SECONDS` | `90`           | URL analysis timeout                     |
| `DOWNLOADER_DOWNLOAD_TIMEOUT_SECONDS` | `3600`         | Download timeout                         |
| `DOWNLOADER_FFMPEG_TIMEOUT_SECONDS`   | `600`          | FFmpeg post-processing timeout           |

The Plex directories are only mount points for now; the Plex destinations themselves
arrive in Milestone 7.

## Current behaviour

Milestones 0–5 are complete; the core download workflow works end to end.

- `GET /api/v1/health` reports API and SQLite database status.
- **URL analysis** — metadata-only yt-dlp analysis behind `POST /api/v1/analysis` with an optional platform hint (Auto Select is the default), normalized media information, source-derived audio/video format choices, and short-lived analysis sessions.
- **Downloads** — single items download to the App Library with live progress (SSE), cancellation and retry; the SQLite-backed queue survives restarts (interrupted jobs fail with a retryable `interrupted_by_restart` error) and respects `DOWNLOADER_DOWNLOAD_CONCURRENCY`.
- **Playlists / albums** — analyzed items can be inspected and selected before the job starts (hard cap: 200 selected items per job) with one collection-level Audio/Video mode and output profile; children run sequentially with per-child results, truthful aggregate counters, and parent-level Cancel/Retry.
- **Activity** — structured per-job activity events via `GET /api/v1/jobs/{id}/activity` and the `GET /api/v1/events` SSE stream.
- **Library** — catalog of completed downloads with size/date/format/path details, Remove from Library, and Delete File with confirmation; files referenced by more than one catalog entry are protected from deletion.

Not implemented yet (see `ARCHITECTURE.md` §59):

- Milestone 6 — embedded metadata, artwork review/embedding, artist/album naming and folder structure.
- Milestone 7 — Plex Music / Plex Videos destinations and Keep a Copy in App Library.
- Milestone 8 — Download to this device.
- The Settings page is still a placeholder; configuration is environment-based.
- One-time v0.1 release security hardening/audit after Milestone 9.
