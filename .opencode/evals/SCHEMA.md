# Evaluation Schema — Generic Workflow Performance Telemetry

**Version:** 1.2
**Scope:** Generic, domain-independent OpenCode workflow telemetry. `categories`
and `tags` are free-form. Storage is JSONL + Markdown only.

## 1. Storage and immutability

`task-runs.jsonl` is append-only: one UTF-8 JSON object per newline. Published
records are immutable execution snapshots. A data repair is a separate
`record_type: "correction"`; it MUST NOT use `execution.rework_of` and is never
an implementation attempt, failure, or rework. Reports live under `reports/`.

All records have:

| Field | Type | Required |
|---|---|---|
| `schema_version` | string (`"1.2"`) | yes |
| `record_type` | `task_run\|milestone_eval\|correction` | yes |
| `record_id` | unique stable string | yes |
| `recorded_at` | ISO-8601 or `null` | yes |
| `capture_mode` | `live\|retrospective` | yes |
| `supersedes_record_id` | record id or `null` | yes |

Unknown required values are represented by `null`, never guessed as zero or
false.

### 1.1 Corrections and the effective dataset

A correction has this payload:

```json
{
  "record_type": "correction",
  "corrects_record_id": "record-id",
  "reason": "string",
  "changes": {"JSON.path": "replacement value"}
}
```

`corrects_record_id` identifies the immutable record whose data is repaired.
Corrections are applied in file order. A correction may itself be superseded by
a later record through `supersedes_record_id`. A replacement task/milestone
snapshot may set `supersedes_record_id` to the old snapshot; the old snapshot
and corrections targeting only that old snapshot are then excluded from metric
rows. The **effective dataset** is: records not superseded by a later effective
record, with non-superseded corrections applied in append order. Broken links,
cycles, duplicate IDs, or invalid paths are reported as limitations and are not
silently resolved. Correction records never count as task records or rework.

`execution.rework_of` remains exclusively a real implementation relationship:
the current bounded task reworked the referenced implementation task.

## 2. `task_run`

Every `task_run` includes all blocks and fields below.

```json
{
  "identity": {
    "project": "string|null", "milestone": "string|null",
    "task_id": "string|null", "title": "string",
    "worker_role": "string|null", "worker_agent": "string|null",
    "model": "string|null", "supervisor_role": "string|null"
  },
  "categories": ["free-form"],
  "tags": ["free-form"],
  "execution": {
    "started_at": "datetime|null", "completed_at": "datetime|null",
    "duration_seconds": "number|null",
    "status": "completed|failed|blocked|open|superseded",
    "implementation_attempts": "int|null",
    "implementation_failures": "int|null",
    "provider_failures": "int|null",
    "provider_failure_types": ["string"]|null,
    "resolver_used": "bool|null", "fallback_used": "bool|null",
    "files_changed": ["path"]|null,
    "rework_of": "record_id|null", "rework_reason": "string|null"
  },
  "acceptance": {
    "accepted_at": "datetime|null",
    "first_pass_accepted": "bool|null",
    "reviewer_required": "bool|null",
    "reviewer_pass_first_try": "bool|null",
    "targeted_tests_passed": "bool|null",
    "critical_findings": "int|null", "important_findings": "int|null",
    "rework_cycles": "int|null", "reopened": "bool|null",
    "scope_violation": "bool|null", "accepted": "bool|null",
    "derivation": "string",
    "findings_addressed": [
      {"id":"string","severity":"critical|important|minor|null",
       "discovery_stage":"implementation|task_review|milestone_review|checker|null"}
    ],
    "checks": [{"name":"string","result":"pass|fail|not_run","evidence":"string|null"}]
  },
  "quality": {
    "discovery_stage": "implementation|task_review|milestone_review|checker|null",
    "regression_detected": "bool|null", "full_gate_failure": "bool|null",
    "closure_retry": "bool|null", "tests_added": "int|null",
    "tests_passing": "int|null", "notes": "string|null"
  },
  "usage": {
    "input_tokens":"int|null", "output_tokens":"int|null",
    "tool_calls":"int|null", "cost":"number|null",
    "currency":"string|null", "source":"metered|estimated|null"
  },
  "risk": {"level":"low|medium|high","basis":"string"},
  "evidence": [{"kind":"git_commit|git_diff|work_state|report|session_log|task_run|other","ref":"string","note":"string|null"}],
  "telemetry": {"captured":["string"],"not_captured":["string"],"notes":"string|null"}
}
```

`duration_seconds` requires real timestamps. Provider/quota/auth/availability
events are canonical only in `execution.provider_*`; they do not increment
implementation attempts/failures and do not trigger resolver escalation.
Bookkeeping itself is not an implementation attempt.

`accepted_at` is the timestamp of authoritative task acceptance, not record
write time or implementation completion. If unavailable it is `null`.

`first_pass_accepted` and `rework_cycles` are compatibility fields, not
authoritative snapshots: writers set them to `null`. Evaluators MUST recompute
them from the effective dataset (§4). Other acceptance fields follow evidence:
milestone closure alone does not prove first-pass acceptance or reviewer result.

Risk is deterministic and evaluated in this priority order:

1. **low** — first preserve low classification when the task is only tests,
   documentation, config/workflow, or a mechanical change **and does not change
   production behavior**.
2. **high** — otherwise, production cancellation/process-lifecycle behavior,
   concurrency/races, persistence or data integrity, security/filesystem safety,
   cross-layer behavior, or critical findings.
3. **medium** — all other single-layer production behavioral changes.

Thus a test-only task with no production change remains low even when it tests a
high-risk domain, while a production cancellation cleanup is high.

## 3. `milestone_eval`

Contains identity, categories, tags, `status: closed|open`, `closed_at`,
`findings_by_stage` (`implementation`, `task_review`, `milestone_review`,
`checker`, each int/null), `findings_total_documented`, `review`
(`pass_on_attempt`, `reopen_count`, `task_review_findings`,
`milestone_review_findings`, `findings_total`, `late_findings`, `notes`),
`closure_checker` (`passed`, `checks_passed`, `checks_total`,
`stage_events_total`, `retries`, `notes`), `resolver_events`,
`provider_failure_events`, `full_gates`, `derived`, evidence, and telemetry.

Reviewer totals include only evidence-proven review discoveries. Checker events
are not reviewer findings. Unknown stage counts remain `null`; do not obtain a
total by pretending unknown is zero. Provider aggregate is derived only from
task-run canonical provider fields.

## 4. Deterministic derivation

Evaluators first construct the effective dataset (§1.1), then derive:

- `rework_cycles(record)` = effective `task_run` count whose
  `execution.rework_of == record.record_id`.
- `first_pass_accepted = false` when an effective implementation rework points
  to the record, or an evidence-proven first review rejected/found an issue;
  `true` only with evidence-proven task-level first-review acceptance and no
  preceding rejection/rework; otherwise `null`.
- `accepted = true` when status is completed and task-level acceptance or
  milestone closure PASS + empty remaining findings covers it; false for
  explicit rejection/open state; otherwise null.
- `reopen_count = review.pass_on_attempt - 1` only when pass attempt is known.
- `time-to-accepted = acceptance.accepted_at - execution.started_at`; report
  average and median for records with both timestamps, otherwise `null`.

The ten required metrics are:

| Metric | Formula | Eligible denominator |
|---|---|---|
| first-pass acceptance rate | true / known boolean first-pass | derived first-pass non-null |
| rework per accepted task | effective task runs with `rework_of` / accepted tasks | derived accepted=true |
| time-to-accepted | accepted_at − started_at (mean/median) | both timestamps present |
| implementation failure rate | Σ failures / Σ attempts | both fields present |
| late finding rate | milestone_review or checker findings / known-stage findings | finding stage non-null |
| reopened finding rate | reopened=true / reopened non-null | reopened evidence present |
| reviewer overturn rate | reviewer_pass_first_try=false / non-null | first review result known |
| checker escape rate | post-checker discoveries / known-stage findings | post-checker monitoring captured |
| scope compliance | scope_violation=false / non-null | scope evidence present |
| provider failure rate | Σ provider_failures / records measured | provider_failures non-null |

Every metric reports value, eligible `n`, and limitation. Nulls never enter a
denominator. `n < 3` is labeled “low n — anecdotal”. Selection bias is stated.
Provider failure rate is separate from quality. Checker escape is `not captured`
unless post-checker monitoring scope exists; never infer zero.

## 5. Evidence and comparison rules

Unknown values are `null` in JSON and `unknown`/`not captured` in reports.
Retrospective claims use only cited durable-state and Git evidence; a report may
not become self-proving. No unsupported numeric or role attribution is allowed.
Duplicate IDs and grouped finding→fix work count once. Model/role comparisons
require model attribution plus comparable risk and category stratification;
retrospective rates are not conclusive performance claims.

## 6. Milestone report format

Reports use exactly these nine headings, in this order:

1. Milestone summary
2. Model/role performance
3. Risk seviyesine göre performans
4. Finding discovery stage dağılımı
5. Rework / reopen / closure retry analizi
6. Provider/infrastructure failures
7. Workflow bottlenecks
8. Routing recommendations
9. Confidence / data limitations

Milestone summary includes all ten metrics with value, eligible n, limitation.
Methodology explains deduplication, grouping, effective-dataset corrections,
and derivation. Routing recommendations are provisional and require explicit
user approval; evaluation never edits routing/config/state.

## 7. Terminal checkpoint capture

Every terminal bounded-task checkpoint (`completed`, `failed`, `blocked`, or
`open`) gets a task-run capture. Successful implementation ordering is:
mechanical WORK_STATE update, Lead read-only verification, telemetry append,
checkpoint. If state update/verification fails, do not claim completion; a
truthful blocked/open telemetry record may still be appended. If no suitable
provider fallback exists, capture the provider event and checkpoint without
marking WORK_STATE completed. Telemetry failure changes neither implementation
acceptance nor milestone state; it is reported as a visible limitation.

## 8. JSONL rules and version history

No trailing commas or raw newlines in strings; final newline required. All
task-run blocks/fields are explicit. Lead prepares content and a mechanical
bookkeeping worker appends it without invention. Appends use the safe append
protocol: the worker writes the verbatim record to
`.opencode/evals/pending-task-run.json` and runs exactly
`node .opencode/evals/append-task-run.mjs --append`. The helper performs strict
v1.2 envelope/payload validation and unique-id checks, opens the target with
`O_NOFOLLOW`, and runs pre-append parse, the single `O_APPEND` write with fsync
and post-append verification through the same open file descriptor, so a path
swap during append cannot split write and verification across inodes. Pending
and target bytes must be valid UTF-8. The helper acquires one fixed lock file
with exclusive create and removes only the lock it acquired during its normal
`finally` cleanup. It never performs automatic stale-lock recovery. If the lock
already exists, append fails closed; an operator must first confirm that no
append process is active and then remove the lock manually. A process crash can
therefore leave a lock that requires manual recovery. Bookkeeping-worker
permission does not allow arbitrary lock removal.
The pending file is a transient handoff buffer, never an authoritative record;
`task-runs.jsonl` is the only authoritative store, and direct manual edits or
read-modify-write of it are forbidden.

- **v1.2:** immutable correction/supersession model and effective dataset;
  `accepted_at`; dynamic first-pass/rework derivation; terminal checkpoint
  capture ordering and provider/state-update failure behavior.
- **v1.1:** expanded execution/acceptance/quality fields and ten metric formulas.
