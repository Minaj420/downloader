# AGENTS.md

This repository is developed with AI coding agents.

Before making changes:

1. Read `PRODUCT_SPEC.md`.
2. Read `ARCHITECTURE.md`.
3. Inspect the existing code before proposing implementation.
4. Do not assume missing requirements. Prefer the documented product behaviour.
5. Do not implement future/backlog features unless explicitly requested.

## Working Rules

- Work in small, reviewable increments.
- Do not attempt to implement the entire product in one task.
- Preserve existing working behaviour.
- Keep changes scoped to the requested milestone.
- Do not introduce unnecessary infrastructure or abstractions.
- Do not add Redis, Celery, PostgreSQL, Kubernetes, microservices, or similar infrastructure unless explicitly requested.
- Keep `yt-dlp` and FFmpeg behind integration boundaries.
- Keep filesystem operations behind the storage layer.
- Do not construct shell command strings from user input.
- Do not expose arbitrary filesystem paths for deletion or execution.
- Do not put business rules only in the frontend.
- Use migrations for database schema changes.
- Add or update tests for changed behaviour.

## Before Declaring a Task Complete

Run the relevant checks.

Backend:

```bash
ruff check .
pytest
```

Run the configured type checker once one exists.

Frontend:

```bash
npm run lint
npm run typecheck
npm test
npm run build
```

Only run commands that are actually configured in the repository. If a command is not yet configured, report that clearly instead of pretending it passed.

For Docker-related changes, verify that the image builds and the application starts successfully when practical.

## Git

- Inspect `git status` before making changes.
- Do not overwrite unrelated user changes.
- Do not commit unless explicitly asked.
- Do not push unless explicitly asked.
- Do not rewrite history unless explicitly asked.

## Communication

When starting a task, briefly state:

- what you found,
- what you intend to change,
- any important assumption.

When finished, report:

- files changed,
- behaviour implemented,
- tests/checks actually run,
- any remaining issue or follow-up.

If the requested implementation conflicts with `PRODUCT_SPEC.md` or `ARCHITECTURE.md`, point out the conflict before silently changing the intended behaviour.