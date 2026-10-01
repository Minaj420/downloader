# AGENTS.md

This repository is developed with AI coding agents.

## Starting a task

1. Read the `PRODUCT_SPEC.md` and `ARCHITECTURE.md` sections that are authoritative for the task at hand; you do not need to re-read whole documents for every task.
2. Inspect the existing code and tests before proposing changes.
3. Do not assume undocumented requirements; do not start backlog/future work unless explicitly requested.
4. If a change would conflict with the specs, surface the conflict before implementing it.

## Working rules

- Work in small, reviewable increments scoped to the requested milestone; preserve existing working behaviour.
- Do not introduce unnecessary infrastructure or abstractions (no Redis, Celery, PostgreSQL, Kubernetes, microservices, or similar unless explicitly requested).
- Keep `yt-dlp` and FFmpeg behind integration boundaries; keep filesystem operations behind the storage layer.
- Never build shell command strings from user input; never expose arbitrary filesystem paths for deletion or execution.
- Do not put business rules only in the frontend; use migrations for database schema changes.

## Validation

- Add or update tests for changed behaviour.
- Small bounded tasks: run only the relevant targeted checks. Milestone/final closure: run the full gates on the current working tree.

Backend: `ruff check .` and `pytest`, plus the configured type checker once one exists.

Frontend: `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`.

Only run commands that are actually configured in the repository; report clearly when a command is not configured instead of pretending it passed. For Docker-related changes, verify that the image builds and the application starts successfully when practical.

## Git

- Inspect `git status` before making changes; do not overwrite unrelated user changes.
- Never commit or push unless explicitly asked; never run reset, clean, checkout, restore, rebase, or rewrite history.

## Communication

- Respond to the user in Turkish unless explicitly asked to use another language.
- When starting a task, briefly state what you found, what you intend to change, and any key assumption.
- When finished, report files changed, behaviour implemented, checks actually run (with results), and any remaining issue or follow-up.
