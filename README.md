# Downloader

Self-hosted media downloader built as a graphical interface around `yt-dlp` and `FFmpeg`.

**Status:** early development — Milestone 0 (foundation) is complete. See `PRODUCT_SPEC.md` for the product vision and `ARCHITECTURE.md` §59 for the milestone plan.

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
│   │   └── api/v1/        # versioned API routes (/api/v1/...)
│   └── tests/
└── frontend/
    ├── src/
    │   ├── app/           # application shell (routing, providers)
    │   ├── api/           # API client and hooks
    │   ├── components/
    │   └── pages/         # New Download / Queue / Library / Settings (placeholders)
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
```

## Docker deployment

```bash
docker compose up --build
```

- Application: <http://localhost:8080>
- Health: <http://localhost:8080/api/v1/health>
- Persistent data is stored under `./data/` (`state/`, `staging/`, `library/`).
- The container runs `alembic upgrade head` on startup, then serves the API and the built frontend on port 8080.
- The image includes `yt-dlp` and `ffmpeg` for later milestones.
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

## Current behaviour (Milestone 0)

- `GET /api/v1/health` reports API and SQLite database status.
- Alembic is configured with a baseline migration; future schema changes must use migrations.
- The React shell provides the four primary sections (New Download, Queue, Library, Settings) as placeholders and a backend health indicator.
- Downloading, yt-dlp analysis, queues, and Plex integration are intentionally not implemented yet.
