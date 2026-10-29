# AGENTS.md

This repository is developed with AI coding agents.

- Current state, next task and open decisions: `STATE.md`.
- Agent roles, task loop, review classes and gates: `docs/WORKFLOW.md`.
- OpenCode lead is the sole orchestrator. Claude Code subscription CLI provides Opus
  read-only advice and scoped implementation through `scripts/claude-agent.sh`:
  Sonnet is the default implementer; lead selects explicit `--model opus` for expert work.
  Before each milestone, obtain Opus advice or explicit user approval to proceed without it.
  Important architecture decisions go to Opus first; product choices go to the user.
  Lead exposes Opus recommendation/reason/alternatives-risk and its own decision.
  GPT reviewer independently reviews both Sonnet and Opus code. One writer, at most one review fix round; no quota/auth retry
  or silent model fallback. CLI permissions are not an OS sandbox (see WORKFLOW).
  Lead announces “Uygulayıcı: Sonnet/Opus; gerekçe...” at each task start. Sonnet handles
  bounded ordinary reasoning; choose Opus upfront for hard/ambiguous work, high-impact
  migrations, transaction/concurrency/ownership, destructive filesystem/path safety/data
  loss, or meaningful API contract/architecture changes. Size/file count is not a criterion.
  Luna handles only mechanical low-risk work. Unclear product behaviour requires user choice.
  On resume, lead explicitly resends the initial requested_model via --model; no silent
  family switch or quota retry. Model changes do not reset the single review fix round.

GPT quota exhausted: the user may start the temporary Claude-only mode (`/claude-only`,
then `/devam-claude`; back with `/normal-mod`). Rules in `docs/WORKFLOW.md`.

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

## Proportionality

- Threat model: a single owner on a private homelab, reached over Tailscale, with occasional access by a few trusted friends. There is no public exposure and no login.
- Match rigor to real impact. Low-likelihood, low-impact issues under this model are recorded as accepted risks (one line in `STATE.md`), not fixed with new machinery.
- Data loss, silent corruption, path escape and destructive filesystem behaviour still get full rigor.
- When a decision is needed from the user, ask in plain Turkish, list the options, and recommend the simplest one that works.

## Deployment intent

- Target: one Docker/Podman container on the owner's homelab (`compose.yml`). Public hosting is out of scope.
- Real deployment practice (non-root container, pinned tool versions, CI) is welcome when it stays in deployment files and does not complicate application code.

## Validation

- Add or update tests for changed behaviour.
- Small bounded tasks: run only the relevant targeted checks. Milestone/final closure: run the full gates on the current working tree.

Backend: `ruff check .`, `mypy app tests` and `pytest`.

Frontend: `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`, `npm run check:contrast`.

Only run commands that are actually configured in the repository; report clearly when a command is not configured instead of pretending it passed. For Docker-related changes, verify that the image builds and the application starts successfully when practical.

## Git

- Inspect `git status` before making changes; do not overwrite unrelated user changes.
- Read-only commands (`status`, `diff`, `log`, `show`, `fetch`) and non-destructive branch switches (`switch`/`checkout` to change branches) are normal workflow operations when the task and permissions call for them.
- The orchestrator (`lead` in OpenCode `/continue`; Claude `/devam` is obsolete) commits each task automatically once it has passed review and its checks unless the task forbids it: stage only that task's files one by one (never `git add .`/`-A`), one commit per task, message describing the behaviour change. Worker agents never commit or edit STATE.md.
- Never push unless the user explicitly asks.
- Never run destructive or history-rewriting operations (`reset --hard`, file-deleting `clean`, force push, change-discarding `restore`, `rebase`, `commit --amend`, or any history rewrite) without an explicit user request.

## Communication

- Respond to the user in Turkish unless explicitly asked to use another language.
- When starting a task, briefly state what you found, what you intend to change, and any key assumption.
- When finished, report files changed, behaviour implemented, checks actually run (with results), and any remaining issue or follow-up.
