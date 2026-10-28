#!/usr/bin/env node
/**
 * Safe append helper for .opencode/evals/task-runs.jsonl.
 *
 * Exact modes only (any other arguments fail):
 *   --append     validate pending-task-run.json next to this script, append it
 *                to task-runs.jsonl as one serialized JSON line, remove pending.
 *   --verify     read-only strict validation of task-runs.jsonl (JSON parse of
 *                every nonempty line, strict v1.2 envelope/payload validation,
 *                unique record ids). Never writes.
 *   --self-test  exercise validation, append and lock refusal against
 *                temp-dir fixtures only; the real task-runs.jsonl and the real
 *                lock file are never touched.
 *
 * All paths are hardcoded relative to this script's directory. No
 * user-supplied paths. The target is opened with O_NOFOLLOW (where available)
 * and pre-append parse, the single O_APPEND write with fsync and the
 * post-append verification all go through the SAME open file descriptor, so a
 * path swap during append cannot split write and verification across inodes.
 * Existing bytes are never rewritten or truncated.
 *
 * The lock file payload contains diagnostic pid and created_at values only.
 * Existing locks are never inspected or automatically removed: append fails
 * closed until an operator confirms no append process is active and manually
 * removes the lock. A process crash can therefore leave a lock behind.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { TextDecoder } from 'node:util';
import { fileURLToPath } from 'node:url';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const PENDING_PATH = path.join(SCRIPT_DIR, 'pending-task-run.json');
const TARGET_PATH = path.join(SCRIPT_DIR, 'task-runs.jsonl');
const LOCK_PATH = path.join(SCRIPT_DIR, 'task-runs.jsonl.lock');

const O_NOFOLLOW = Number.isInteger(fs.constants.O_NOFOLLOW) ? fs.constants.O_NOFOLLOW : 0;
const UTF8_DECODER = new TextDecoder('utf-8', { fatal: true });

const RECORD_TYPES = new Set(['task_run', 'milestone_eval', 'correction']);
const EXEC_STATUSES = ['completed', 'failed', 'blocked', 'open', 'superseded'];
const DISCOVERY_STAGES = ['implementation', 'task_review', 'milestone_review', 'checker'];
const SEVERITIES = ['critical', 'important', 'minor'];
const CHECK_RESULTS = ['pass', 'fail', 'not_run'];
const RISK_LEVELS = ['low', 'medium', 'high'];
const EVIDENCE_KINDS = [
  'git_commit',
  'git_diff',
  'work_state',
  'report',
  'session_log',
  'task_run',
  'other',
];
const USAGE_SOURCES = ['metered', 'estimated'];
const MILESTONE_STATUSES = ['closed', 'open'];
const GATE_RESULTS = ['pass', 'fail'];

class OpError extends Error {}

function fail(msg) {
  throw new OpError(msg);
}

/* ------------------------------ type helpers ------------------------------ */

function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasOwn(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

const isStr = (v) => typeof v === 'string';
const isNonEmptyStr = (v) => typeof v === 'string' && v.length > 0;
const isBool = (v) => typeof v === 'boolean';
const isStrOrNull = (v) => v === null || typeof v === 'string';
const isNonEmptyStrOrNull = (v) => v === null || isNonEmptyStr(v);
const isBoolOrNull = (v) => v === null || typeof v === 'boolean';
const isNonNegInt = (v) => Number.isInteger(v) && v >= 0;
const isNonNegIntOrNull = (v) => v === null || isNonNegInt(v);
const isNonNegNumOrNull = (v) =>
  v === null || (typeof v === 'number' && Number.isFinite(v) && v >= 0);
const isStrArray = (v) => Array.isArray(v) && v.every((x) => typeof x === 'string');
const isStrArrayOrNull = (v) => v === null || isStrArray(v);
const oneOf = (values) => (v) => values.includes(v);
const nullableOneOf = (values) => (v) => v === null || values.includes(v);

const ISO_DATE_RE =
  /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;

function isIsoString(v) {
  return typeof v === 'string' && ISO_DATE_RE.test(v) && !Number.isNaN(Date.parse(v));
}

const isIsoOrNull = (v) => v === null || isIsoString(v);

/* --------------------------- strict v1.2 validator ------------------------ */

function requireField(obj, key, check, label, desc) {
  if (!hasOwn(obj, key)) fail(`${label}: missing required field "${key}"`);
  if (!check(obj[key])) fail(`${label}: field "${key}" must be ${desc}`);
}

function validateObjectBlock(record, blockName, fields, label) {
  const block = record[blockName];
  if (!isPlainObject(block)) fail(`${label}: block "${blockName}" must be an object`);
  for (const [name, [check, desc]] of Object.entries(fields)) {
    requireField(block, name, check, `${label}: block "${blockName}"`, desc);
  }
}

function validateArrayBlock(record, blockName, itemValidator, label) {
  const block = record[blockName];
  if (!Array.isArray(block)) fail(`${label}: block "${blockName}" must be an array`);
  block.forEach((item, index) => itemValidator(item, `${label}: ${blockName}[${index}]`));
}

function validateNestedArray(owner, ownerName, key, itemValidator, label) {
  const value = owner[key];
  if (!Array.isArray(value)) fail(`${label}: ${ownerName}."${key}" must be an array`);
  value.forEach((item, index) => itemValidator(item, `${label}: ${ownerName}.${key}[${index}]`));
}

function validateFindingAddressed(item, label) {
  if (!isPlainObject(item)) fail(`${label}: must be an object`);
  requireField(item, 'id', isNonEmptyStr, label, 'a nonempty string');
  requireField(
    item,
    'severity',
    nullableOneOf(SEVERITIES),
    label,
    `null or one of ${SEVERITIES.join('|')}`,
  );
  requireField(
    item,
    'discovery_stage',
    nullableOneOf(DISCOVERY_STAGES),
    label,
    `null or one of ${DISCOVERY_STAGES.join('|')}`,
  );
}

function validateCheckItem(item, label) {
  if (!isPlainObject(item)) fail(`${label}: must be an object`);
  requireField(item, 'name', isNonEmptyStr, label, 'a nonempty string');
  requireField(item, 'result', oneOf(CHECK_RESULTS), label, `one of ${CHECK_RESULTS.join('|')}`);
  requireField(item, 'evidence', isStrOrNull, label, 'a string or null');
}

function validateEvidenceItem(item, label) {
  if (!isPlainObject(item)) fail(`${label}: must be an object`);
  requireField(item, 'kind', oneOf(EVIDENCE_KINDS), label, `one of ${EVIDENCE_KINDS.join('|')}`);
  requireField(item, 'ref', isNonEmptyStr, label, 'a nonempty string');
  requireField(item, 'note', isStrOrNull, label, 'a string or null');
}

function validateGateItem(item, label) {
  if (!isPlainObject(item)) fail(`${label}: must be an object`);
  requireField(item, 'name', isNonEmptyStr, label, 'a nonempty string');
  requireField(item, 'result', oneOf(GATE_RESULTS), label, `one of ${GATE_RESULTS.join('|')}`);
  requireField(item, 'detail', isStrOrNull, label, 'a string or null');
}

function validateTelemetryBlock(record, label) {
  validateObjectBlock(
    record,
    'telemetry',
    {
      captured: [isStrArray, 'an array of strings'],
      not_captured: [isStrArray, 'an array of strings'],
      notes: [isStrOrNull, 'a string or null'],
    },
    label,
  );
}

function validateTaskRun(record, label) {
  validateObjectBlock(
    record,
    'identity',
    {
      project: [isStrOrNull, 'a string or null'],
      milestone: [isStrOrNull, 'a string or null'],
      task_id: [isStrOrNull, 'a string or null'],
      title: [isNonEmptyStr, 'a nonempty string'],
      worker_role: [isStrOrNull, 'a string or null'],
      worker_agent: [isStrOrNull, 'a string or null'],
      model: [isStrOrNull, 'a string or null'],
      supervisor_role: [isStrOrNull, 'a string or null'],
    },
    label,
  );
  validateArrayBlock(record, 'categories', (item, itemLabel) => {
    if (!isStr(item)) fail(`${itemLabel}: must be a string`);
  }, label);
  validateArrayBlock(record, 'tags', (item, itemLabel) => {
    if (!isStr(item)) fail(`${itemLabel}: must be a string`);
  }, label);
  validateObjectBlock(
    record,
    'execution',
    {
      started_at: [isIsoOrNull, 'an ISO-8601 string or null'],
      completed_at: [isIsoOrNull, 'an ISO-8601 string or null'],
      duration_seconds: [isNonNegNumOrNull, 'a nonnegative finite number or null'],
      status: [oneOf(EXEC_STATUSES), `one of ${EXEC_STATUSES.join('|')}`],
      implementation_attempts: [isNonNegIntOrNull, 'a nonnegative int or null'],
      implementation_failures: [isNonNegIntOrNull, 'a nonnegative int or null'],
      provider_failures: [isNonNegIntOrNull, 'a nonnegative int or null'],
      provider_failure_types: [isStrArrayOrNull, 'an array of strings or null'],
      resolver_used: [isBoolOrNull, 'a boolean or null'],
      fallback_used: [isBoolOrNull, 'a boolean or null'],
      files_changed: [isStrArrayOrNull, 'an array of strings or null'],
      rework_of: [isNonEmptyStrOrNull, 'a nonempty string or null'],
      rework_reason: [isStrOrNull, 'a string or null'],
    },
    label,
  );
  validateObjectBlock(
    record,
    'acceptance',
    {
      accepted_at: [isIsoOrNull, 'an ISO-8601 string or null'],
      first_pass_accepted: [isBoolOrNull, 'a boolean or null'],
      reviewer_required: [isBoolOrNull, 'a boolean or null'],
      reviewer_pass_first_try: [isBoolOrNull, 'a boolean or null'],
      targeted_tests_passed: [isBoolOrNull, 'a boolean or null'],
      critical_findings: [isNonNegIntOrNull, 'a nonnegative int or null'],
      important_findings: [isNonNegIntOrNull, 'a nonnegative int or null'],
      rework_cycles: [isNonNegIntOrNull, 'a nonnegative int or null'],
      reopened: [isBoolOrNull, 'a boolean or null'],
      scope_violation: [isBoolOrNull, 'a boolean or null'],
      accepted: [isBoolOrNull, 'a boolean or null'],
      derivation: [isNonEmptyStr, 'a nonempty string'],
    },
    label,
  );
  validateNestedArray(record.acceptance, 'acceptance', 'findings_addressed', validateFindingAddressed, label);
  validateNestedArray(record.acceptance, 'acceptance', 'checks', validateCheckItem, label);
  validateObjectBlock(
    record,
    'quality',
    {
      discovery_stage: [nullableOneOf(DISCOVERY_STAGES), `null or one of ${DISCOVERY_STAGES.join('|')}`],
      regression_detected: [isBoolOrNull, 'a boolean or null'],
      full_gate_failure: [isBoolOrNull, 'a boolean or null'],
      closure_retry: [isBoolOrNull, 'a boolean or null'],
      tests_added: [isNonNegIntOrNull, 'a nonnegative int or null'],
      tests_passing: [isNonNegIntOrNull, 'a nonnegative int or null'],
      notes: [isStrOrNull, 'a string or null'],
    },
    label,
  );
  validateObjectBlock(
    record,
    'usage',
    {
      input_tokens: [isNonNegIntOrNull, 'a nonnegative int or null'],
      output_tokens: [isNonNegIntOrNull, 'a nonnegative int or null'],
      tool_calls: [isNonNegIntOrNull, 'a nonnegative int or null'],
      cost: [isNonNegNumOrNull, 'a nonnegative finite number or null'],
      currency: [isStrOrNull, 'a string or null'],
      source: [nullableOneOf(USAGE_SOURCES), `null or one of ${USAGE_SOURCES.join('|')}`],
    },
    label,
  );
  validateObjectBlock(
    record,
    'risk',
    {
      level: [oneOf(RISK_LEVELS), `one of ${RISK_LEVELS.join('|')}`],
      basis: [isNonEmptyStr, 'a nonempty string'],
    },
    label,
  );
  validateArrayBlock(record, 'evidence', validateEvidenceItem, label);
  validateTelemetryBlock(record, label);
}

function validateMilestoneEval(record, label) {
  validateObjectBlock(
    record,
    'identity',
    {
      project: [isStrOrNull, 'a string or null'],
      milestone: [isStrOrNull, 'a string or null'],
      title: [isNonEmptyStr, 'a nonempty string'],
    },
    label,
  );
  validateArrayBlock(record, 'categories', (item, itemLabel) => {
    if (!isStr(item)) fail(`${itemLabel}: must be a string`);
  }, label);
  validateArrayBlock(record, 'tags', (item, itemLabel) => {
    if (!isStr(item)) fail(`${itemLabel}: must be a string`);
  }, label);
  requireField(
    record,
    'status',
    oneOf(MILESTONE_STATUSES),
    label,
    `one of ${MILESTONE_STATUSES.join('|')}`,
  );
  requireField(record, 'closed_at', isIsoOrNull, label, 'an ISO-8601 string or null');
  requireField(
    record,
    'findings_total_documented',
    isNonNegIntOrNull,
    label,
    'a nonnegative int or null',
  );
  validateObjectBlock(
    record,
    'findings_by_stage',
    {
      implementation: [isNonNegIntOrNull, 'a nonnegative int or null'],
      task_review: [isNonNegIntOrNull, 'a nonnegative int or null'],
      milestone_review: [isNonNegIntOrNull, 'a nonnegative int or null'],
      checker: [isNonNegIntOrNull, 'a nonnegative int or null'],
    },
    label,
  );
  validateObjectBlock(
    record,
    'review',
    {
      pass_on_attempt: [isNonNegIntOrNull, 'a nonnegative int or null'],
      reopen_count: [isNonNegIntOrNull, 'a nonnegative int or null'],
      task_review_findings: [isNonNegIntOrNull, 'a nonnegative int or null'],
      milestone_review_findings: [isNonNegIntOrNull, 'a nonnegative int or null'],
      findings_total: [isNonNegIntOrNull, 'a nonnegative int or null'],
      late_findings: [isStrArrayOrNull, 'an array of strings or null'],
      notes: [isStrOrNull, 'a string or null'],
    },
    label,
  );
  validateObjectBlock(
    record,
    'closure_checker',
    {
      passed: [isBool, 'a boolean'],
      checks_passed: [isNonNegIntOrNull, 'a nonnegative int or null'],
      checks_total: [isNonNegIntOrNull, 'a nonnegative int or null'],
      stage_events_total: [isNonNegIntOrNull, 'a nonnegative int or null'],
      retries: [isNonNegIntOrNull, 'a nonnegative int or null'],
      notes: [isStrOrNull, 'a string or null'],
    },
    label,
  );
  requireField(
    record,
    'resolver_events',
    isNonNegIntOrNull,
    label,
    'a nonnegative int or null',
  );
  requireField(
    record,
    'provider_failure_events',
    isNonNegIntOrNull,
    label,
    'a nonnegative int or null',
  );
  validateArrayBlock(record, 'full_gates', validateGateItem, label);
  if (!isPlainObject(record.derived)) {
    fail(`${label}: field "derived" must be an object`);
  }
  validateArrayBlock(record, 'evidence', validateEvidenceItem, label);
  validateTelemetryBlock(record, label);
}

function validateCorrection(record, label) {
  requireField(record, 'corrects_record_id', isNonEmptyStr, label, 'a nonempty string');
  requireField(record, 'reason', isNonEmptyStr, label, 'a nonempty string');
  if (!isPlainObject(record.changes)) {
    fail(`${label}: field "changes" must be an object`);
  }
  if (Object.keys(record.changes).length === 0) {
    fail(`${label}: field "changes" must not be empty`);
  }
}

/**
 * Strict v1.2 record validation. Returns the record_id.
 * Common envelope for every record_type; payload validation per type.
 */
function validateRecord(record, label) {
  if (!isPlainObject(record)) {
    fail(`${label}: top-level JSON value must be an object (arrays are not records)`);
  }
  if (record.schema_version !== '1.2') {
    fail(`${label}: schema_version must be exactly "1.2"`);
  }
  if (!RECORD_TYPES.has(record.record_type)) {
    fail(`${label}: record_type must be one of ${[...RECORD_TYPES].join(', ')}`);
  }
  requireField(record, 'record_id', isNonEmptyStr, label, 'a nonempty string');
  requireField(record, 'recorded_at', isIsoOrNull, label, 'an ISO-8601 string or null');
  requireField(record, 'capture_mode', oneOf(['live', 'retrospective']), label, 'live or retrospective');
  requireField(record, 'supersedes_record_id', isNonEmptyStrOrNull, label, 'a nonempty string or null');

  if (record.record_type === 'task_run') validateTaskRun(record, label);
  else if (record.record_type === 'milestone_eval') validateMilestoneEval(record, label);
  else validateCorrection(record, label);

  return record.record_id;
}

/* ------------------------------- file helpers ------------------------------ */

/**
 * Read the whole file content through an already-open fd using positional
 * reads (position starts at 0 regardless of the fd offset).
 */
function readAllFromFd(fd) {
  const chunks = [];
  const chunk = Buffer.alloc(65536);
  let position = 0;
  for (;;) {
    const bytesRead = fs.readSync(fd, chunk, 0, chunk.length, position);
    if (bytesRead === 0) break;
    chunks.push(Buffer.from(chunk.subarray(0, bytesRead)));
    position += bytesRead;
  }
  return Buffer.concat(chunks);
}

function decodeUtf8(raw, label) {
  try {
    return UTF8_DECODER.decode(raw);
  } catch (err) {
    fail(`${label} is not valid UTF-8 (${err.message})`);
  }
}

/**
 * Open the target with O_NOFOLLOW (where available) and verify it is a
 * regular file. The target must already exist.
 */
function openTarget(targetPath, baseFlags) {
  let fd;
  try {
    fd = fs.openSync(targetPath, baseFlags | O_NOFOLLOW);
  } catch (err) {
    if (err.code === 'ELOOP') fail(`target is a symlink, refusing: ${targetPath}`);
    if (err.code === 'ENOENT') fail(`target not found: ${targetPath}`);
    throw err;
  }
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile()) fail(`target is not a regular file: ${targetPath}`);
    return fd;
  } catch (err) {
    fs.closeSync(fd);
    throw err;
  }
}

/**
 * Strict read-only parse of raw JSONL bytes. Rejects a missing final newline,
 * empty lines, invalid JSON, invalid strict envelopes and duplicate record ids.
 */
function parseJsonlBuffer(raw) {
  const text = decodeUtf8(raw, 'target');
  if (raw.length > 0 && raw[raw.length - 1] !== 0x0a) {
    fail('target does not end with a newline; refusing to append to a partial line');
  }
  const lines = text.split('\n');
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
  const ids = new Map();
  lines.forEach((line, index) => {
    const label = `line ${index + 1}`;
    if (line.trim() === '') fail(`${label}: empty lines are not allowed`);
    let record;
    try {
      record = JSON.parse(line);
    } catch (err) {
      fail(`${label}: invalid JSON (${err.message})`);
    }
    const recordId = validateRecord(record, label);
    if (ids.has(recordId)) fail(`${label}: duplicate record_id "${recordId}"`);
    ids.set(recordId, 1);
  });
  return { count: lines.length, ids };
}

/**
 * Read and validate the pending record. Rejects symlinks and non-regular
 * files (lstat plus O_NOFOLLOW open where available), parses UTF-8 JSON and
 * validates the strict envelope. Never writes anything.
 */
function readPendingRecord(pendingPath) {
  let stat;
  try {
    stat = fs.lstatSync(pendingPath);
  } catch {
    fail(`pending file not found: ${pendingPath}`);
  }
  if (stat.isSymbolicLink()) fail(`pending file is a symlink, refusing: ${pendingPath}`);
  if (!stat.isFile()) fail(`pending file is not a regular file: ${pendingPath}`);
  let fd;
  try {
    fd = fs.openSync(pendingPath, fs.constants.O_RDONLY | O_NOFOLLOW);
  } catch (err) {
    if (err.code === 'ELOOP') fail(`pending file is a symlink, refusing: ${pendingPath}`);
    throw err;
  }
  let text;
  try {
    const fstat = fs.fstatSync(fd);
    if (!fstat.isFile()) fail(`pending file is not a regular file: ${pendingPath}`);
    text = decodeUtf8(readAllFromFd(fd), 'pending file');
  } finally {
    fs.closeSync(fd);
  }
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  let record;
  try {
    record = JSON.parse(text);
  } catch (err) {
    fail(`pending file is not valid JSON: ${err.message}`);
  }
  const recordId = validateRecord(record, 'pending record');
  return { record, recordId };
}

/* --------------------------------- locking -------------------------------- */

function makeLockPayload() {
  return {
    pid: process.pid,
    created_at: new Date().toISOString(),
  };
}

/** Become the lock owner via exclusive create; any existing lock fails closed. */
function acquireLock(lockPath) {
  let fd;
  try {
    fd = fs.openSync(lockPath, 'wx');
  } catch (err) {
    if (err.code === 'EEXIST') {
      fail(
        `lock exists; manual recovery required after confirming no append process is active: ${lockPath}`,
      );
    }
    throw err;
  }
  try {
    const payload = makeLockPayload();
    const data = Buffer.from(JSON.stringify(payload), 'utf8');
    const written = fs.writeSync(fd, data);
    if (written !== data.length) fail('short write while creating lock file');
    fs.fsyncSync(fd);
    return true;
  } finally {
    fs.closeSync(fd);
  }
}

/** Remove the fixed lock acquired by this invocation during normal cleanup. */
function releaseLock(lockPath) {
  try {
    fs.unlinkSync(lockPath);
  } catch (err) {
    if (err.code !== 'ENOENT') console.error(`warning: failed to release lock ${lockPath}: ${err.message}`);
  }
}

/* ------------------------------ core append ------------------------------- */

/**
 * Core append routine. Paths are injected so --self-test can point it at a
 * temp directory; the CLI always passes the hardcoded script-relative paths.
 * All target reads, the append write and the post-append verification go
 * through ONE fd opened with O_RDWR|O_APPEND|O_NOFOLLOW under the lock.
 */
function appendTaskRun({ targetPath, pendingPath, lockPath }) {
  // Fail-fast pre-checks (outside the lock).
  readPendingRecord(pendingPath);
  {
    const fd = openTarget(targetPath, fs.constants.O_RDONLY);
    try {
      parseJsonlBuffer(readAllFromFd(fd));
    } finally {
      fs.closeSync(fd);
    }
  }

  acquireLock(lockPath);
  let fd;
  try {
    fd = openTarget(targetPath, fs.constants.O_RDWR | fs.constants.O_APPEND);
    const before = readAllFromFd(fd);
    const state = parseJsonlBuffer(before);
    // Re-verify the pending handoff file under the lock (no-follow read).
    const pending = readPendingRecord(pendingPath);
    if (state.ids.has(pending.recordId)) {
      fail(`record_id "${pending.recordId}" already exists; refusing duplicate append`);
    }

    const line = Buffer.from(`${JSON.stringify(pending.record)}\n`, 'utf8');
    const written = fs.writeSync(fd, line); // O_APPEND: single write at EOF
    if (written !== line.length) {
      fail(`short write: ${written}/${line.length} bytes; manual inspection required`);
    }
    fs.fsyncSync(fd);

    // Post-append verification through the SAME fd/inode: strict full parse
    // plus byte-proof that the file is exactly previous bytes + new record.
    const after = readAllFromFd(fd);
    const afterState = parseJsonlBuffer(after);
    const occurrences = afterState.ids.get(pending.recordId) || 0;
    if (occurrences !== 1) {
      fail(
        `post-append verification failed: record_id "${pending.recordId}" occurs ${occurrences} times; manual inspection required`,
      );
    }
    const exactContent =
      after.length === before.length + line.length &&
      after.subarray(0, before.length).equals(before) &&
      after.subarray(before.length).equals(line);
    if (!exactContent) {
      fail(
        'post-append verification failed: file content is not exactly the previous bytes followed by the new record; manual inspection required',
      );
    }

    // Success: remove the pending handoff file.
    fs.unlinkSync(pendingPath);
    return { recordId: pending.recordId, bytes: line.length, totalRecords: afterState.count };
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        /* best effort; lock release must still happen */
      }
    }
    releaseLock(lockPath);
  }
}

/* ------------------------------ verification ------------------------------ */

function verifyTarget(targetPath) {
  const fd = openTarget(targetPath, fs.constants.O_RDONLY);
  try {
    return parseJsonlBuffer(readAllFromFd(fd));
  } finally {
    fs.closeSync(fd);
  }
}

/* -------------------------------- self-test ------------------------------- */

function selfTestAssert(condition, message) {
  if (!condition) fail(`self-test failed: ${message}`);
}

function variant(base, mutate) {
  const clone = JSON.parse(JSON.stringify(base));
  mutate(clone);
  return clone;
}

function expectFailure(fn, label) {
  try {
    fn();
  } catch (err) {
    if (err instanceof OpError) return err.message;
    throw err;
  }
  fail(`self-test failed: ${label} was expected to fail but succeeded`);
  return '';
}

function dummyTaskRun(recordId, extra) {
  const record = {
    schema_version: '1.2',
    record_type: 'task_run',
    record_id: recordId,
    recorded_at: null,
    capture_mode: 'retrospective',
    supersedes_record_id: null,
    identity: {
      project: 'selftest',
      milestone: null,
      task_id: null,
      title: 'selftest record',
      worker_role: null,
      worker_agent: null,
      model: null,
      supervisor_role: null,
    },
    categories: ['selftest'],
    tags: [],
    execution: {
      started_at: null,
      completed_at: null,
      duration_seconds: null,
      status: 'completed',
      implementation_attempts: null,
      implementation_failures: null,
      provider_failures: null,
      provider_failure_types: null,
      resolver_used: null,
      fallback_used: null,
      files_changed: null,
      rework_of: null,
      rework_reason: null,
    },
    acceptance: {
      accepted_at: null,
      first_pass_accepted: null,
      reviewer_required: null,
      reviewer_pass_first_try: null,
      targeted_tests_passed: null,
      critical_findings: null,
      important_findings: null,
      rework_cycles: null,
      reopened: null,
      scope_violation: null,
      accepted: null,
      derivation: 'selftest',
      findings_addressed: [],
      checks: [],
    },
    quality: {
      discovery_stage: null,
      regression_detected: null,
      full_gate_failure: null,
      closure_retry: null,
      tests_added: null,
      tests_passing: null,
      notes: null,
    },
    usage: {
      input_tokens: null,
      output_tokens: null,
      tool_calls: null,
      cost: null,
      currency: null,
      source: null,
    },
    risk: { level: 'low', basis: 'selftest' },
    evidence: [],
    telemetry: { captured: [], not_captured: [], notes: null },
  };
  return Object.assign(record, extra || {});
}

function dummyMilestoneEval(recordId) {
  return {
    schema_version: '1.2',
    record_type: 'milestone_eval',
    record_id: recordId,
    recorded_at: null,
    capture_mode: 'retrospective',
    supersedes_record_id: null,
    identity: { project: 'selftest', milestone: 'ST', title: 'selftest milestone eval' },
    categories: ['milestone-closure'],
    tags: ['selftest'],
    status: 'closed',
    closed_at: null,
    findings_by_stage: { implementation: null, task_review: null, milestone_review: 0, checker: 0 },
    findings_total_documented: 0,
    review: {
      pass_on_attempt: 1,
      reopen_count: 0,
      task_review_findings: null,
      milestone_review_findings: 0,
      findings_total: null,
      late_findings: [],
      notes: null,
    },
    closure_checker: {
      passed: true,
      checks_passed: 1,
      checks_total: 1,
      stage_events_total: 0,
      retries: null,
      notes: null,
    },
    resolver_events: 0,
    provider_failure_events: null,
    full_gates: [{ name: 'selftest gate', result: 'pass', detail: null }],
    derived: { task_records: 1, accepted_tasks: 1, rework_records: 0 },
    evidence: [{ kind: 'work_state', ref: 'selftest', note: null }],
    telemetry: { captured: [], not_captured: [], notes: null },
  };
}

function dummyCorrection(recordId) {
  return {
    schema_version: '1.2',
    record_type: 'correction',
    record_id: recordId,
    recorded_at: null,
    capture_mode: 'live',
    supersedes_record_id: null,
    corrects_record_id: 'st-1',
    reason: 'selftest correction',
    changes: { 'execution.status': 'failed' },
  };
}

function runValidatorSuite() {
  const taskRun = dummyTaskRun('vt-1');
  const milestone = dummyMilestoneEval('vt-2');
  const correction = dummyCorrection('vt-3');
  for (const [name, record] of [
    ['task_run', taskRun],
    ['milestone_eval', milestone],
    ['correction', correction],
  ]) {
    selfTestAssert(validateRecord(record, `valid ${name}`) === record.record_id, `valid ${name} fixture must pass`);
  }

  const invalidTaskRunMutations = [
    ['schema_version 1.1', (r) => { r.schema_version = '1.1'; }],
    ['schema_version 2.0', (r) => { r.schema_version = '2.0'; }],
    ['empty record_id', (r) => { r.record_id = ''; }],
    ['bad capture_mode', (r) => { r.capture_mode = 'mixed'; }],
    ['non-string recorded_at', (r) => { r.recorded_at = 5; }],
    ['non-string supersedes_record_id', (r) => { r.supersedes_record_id = 7; }],
    ['null identity.title', (r) => { r.identity.title = null; }],
    ['numeric identity.worker_agent', (r) => { r.identity.worker_agent = 3; }],
    ['non-string categories item', (r) => { r.categories = ['ok', 2]; }],
    ['bad execution.status enum', (r) => { r.execution.status = 'done'; }],
    ['negative implementation_attempts', (r) => { r.execution.implementation_attempts = -1; }],
    ['string duration_seconds', (r) => { r.execution.duration_seconds = 'fast'; }],
    ['missing execution block', (r) => { delete r.execution; }],
    ['bad usage.source enum', (r) => { r.usage.source = 'guess'; }],
    ['bad risk.level enum', (r) => { r.risk.level = 'extreme'; }],
    ['null telemetry.captured', (r) => { r.telemetry.captured = null; }],
    ['bad evidence kind enum', (r) => { r.evidence = [{ kind: 'email', ref: 'r', note: null }]; }],
    ['bad check result enum', (r) => { r.acceptance.checks = [{ name: 'c', result: 'maybe', evidence: null }]; }],
    ['empty findings_addressed id', (r) => { r.acceptance.findings_addressed = [{ id: '', severity: null, discovery_stage: null }]; }],
    ['bad quality discovery_stage enum', (r) => { r.quality.discovery_stage = 'prod'; }],
  ];
  for (const [name, mutate] of invalidTaskRunMutations) {
    const record = variant(taskRun, mutate);
    expectFailure(() => validateRecord(record, 'fixture'), `task_run with ${name}`);
  }

  const invalidMilestoneMutations = [
    ['bad status enum', (r) => { r.status = 'paused'; }],
    ['null closure_checker.passed', (r) => { r.closure_checker.passed = null; }],
    ['missing findings_by_stage.checker', (r) => { delete r.findings_by_stage.checker; }],
    ['string late_findings', (r) => { r.review.late_findings = 'I9'; }],
    ['missing derived block', (r) => { delete r.derived; }],
    ['bad full_gate result enum', (r) => { r.full_gates = [{ name: 'g', result: 'maybe', detail: null }]; }],
  ];
  for (const [name, mutate] of invalidMilestoneMutations) {
    const record = variant(milestone, mutate);
    expectFailure(() => validateRecord(record, 'fixture'), `milestone_eval with ${name}`);
  }

  const invalidCorrectionMutations = [
    ['empty changes', (r) => { r.changes = {}; }],
    ['empty reason', (r) => { r.reason = ''; }],
    ['empty corrects_record_id', (r) => { r.corrects_record_id = ''; }],
    ['missing changes', (r) => { delete r.changes; }],
  ];
  for (const [name, mutate] of invalidCorrectionMutations) {
    const record = variant(correction, mutate);
    expectFailure(() => validateRecord(record, 'fixture'), `correction with ${name}`);
  }

  expectFailure(() => validateRecord([taskRun], 'fixture'), 'top-level array');
}

function runSelfTest() {
  runValidatorSuite();

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'append-task-run-selftest-'));
  try {
    const targetPath = path.join(dir, 'task-runs.jsonl');
    const lockPath = path.join(dir, 'task-runs.jsonl.lock');
    const pendingPath = path.join(dir, 'pending-task-run.json');

    // Fixture: two existing records, first line longer than 2000 chars.
    const longRecord = dummyTaskRun('st-1', {
      telemetry: { captured: [], not_captured: [], notes: 'x'.repeat(2200) },
    });
    const shortRecord = dummyTaskRun('st-2');
    selfTestAssert(JSON.stringify(longRecord).length > 2000, 'fixture line must exceed 2000 chars');
    const beforeBuffer = Buffer.from(
      `${JSON.stringify(longRecord)}\n${JSON.stringify(shortRecord)}\n`,
      'utf8',
    );
    fs.writeFileSync(targetPath, beforeBuffer);

    // Happy path append through the shared routine.
    const pendingRecord = dummyTaskRun('st-3');
    fs.writeFileSync(pendingPath, JSON.stringify(pendingRecord, null, 2));
    const result = appendTaskRun({ targetPath, pendingPath, lockPath });

    const afterBuffer = fs.readFileSync(targetPath);
    selfTestAssert(afterBuffer.length > beforeBuffer.length, 'file must grow after append');
    selfTestAssert(
      afterBuffer.subarray(0, beforeBuffer.length).equals(beforeBuffer),
      'previous bytes must remain an exact byte-for-byte prefix',
    );
    const expectedTail = Buffer.from(`${JSON.stringify(pendingRecord)}\n`, 'utf8');
    selfTestAssert(
      afterBuffer.subarray(beforeBuffer.length).equals(expectedTail),
      'append must add exactly one serialized object plus newline',
    );
    const afterState = parseJsonlBuffer(fs.readFileSync(targetPath));
    selfTestAssert(afterState.count === 3, 'all JSONL lines must pass strict parse after append');
    selfTestAssert(
      (afterState.ids.get('st-3') || 0) === 1,
      'new record_id must occur exactly once',
    );
    selfTestAssert(!fs.existsSync(pendingPath), 'pending must be removed on success');
    selfTestAssert(!fs.existsSync(lockPath), 'lock must be removed on success');
    selfTestAssert(result.recordId === 'st-3' && result.totalRecords === 3, 'result summary');

    // Failure: duplicate record_id (pending must survive, no append).
    fs.writeFileSync(pendingPath, JSON.stringify(dummyTaskRun('st-3')));
    expectFailure(() => appendTaskRun({ targetPath, pendingPath, lockPath }), 'duplicate append');
    selfTestAssert(fs.existsSync(pendingPath), 'pending must survive a failed append');
    selfTestAssert(fs.readFileSync(targetPath).length === afterBuffer.length, 'no bytes added');
    selfTestAssert(!fs.existsSync(lockPath), 'lock must be released after failure');

    // Failure: missing trailing newline on target.
    const noNewlineTarget = path.join(dir, 'no-newline.jsonl');
    fs.writeFileSync(noNewlineTarget, JSON.stringify(dummyTaskRun('st-9')));
    fs.writeFileSync(pendingPath, JSON.stringify(dummyTaskRun('st-4')));
    expectFailure(
      () => appendTaskRun({ targetPath: noNewlineTarget, pendingPath, lockPath }),
      'append to file without final newline',
    );
    selfTestAssert(fs.existsSync(pendingPath), 'pending must survive newline rejection');

    // Failure: pending file missing.
    fs.unlinkSync(pendingPath);
    expectFailure(() => appendTaskRun({ targetPath, pendingPath, lockPath }), 'missing pending');

    // Failure: pending file is a symlink.
    fs.writeFileSync(path.join(dir, 'real-pending.json'), JSON.stringify(dummyTaskRun('st-5')));
    fs.symlinkSync(path.join(dir, 'real-pending.json'), pendingPath);
    expectFailure(() => appendTaskRun({ targetPath, pendingPath, lockPath }), 'symlinked pending');
    fs.unlinkSync(pendingPath);

    // Failure: invalid JSON in pending.
    fs.writeFileSync(pendingPath, '{"schema_version": "1.2", oops');
    expectFailure(() => appendTaskRun({ targetPath, pendingPath, lockPath }), 'invalid JSON pending');

    // Failure: pending bytes are not valid UTF-8.
    fs.writeFileSync(pendingPath, Buffer.from([0xff]));
    const invalidPendingMessage = expectFailure(
      () => appendTaskRun({ targetPath, pendingPath, lockPath }),
      'invalid UTF-8 pending',
    );
    selfTestAssert(
      invalidPendingMessage.includes('pending file is not valid UTF-8'),
      'invalid UTF-8 pending must fail explicitly',
    );

    // Failure: top-level array in pending.
    fs.writeFileSync(pendingPath, JSON.stringify([dummyTaskRun('st-6')]));
    expectFailure(() => appendTaskRun({ targetPath, pendingPath, lockPath }), 'array pending');

    // Failure: target bytes are not valid UTF-8.
    const invalidUtf8Target = path.join(dir, 'invalid-utf8.jsonl');
    fs.writeFileSync(invalidUtf8Target, Buffer.concat([beforeBuffer, Buffer.from([0xff, 0x0a])]));
    fs.writeFileSync(pendingPath, JSON.stringify(dummyTaskRun('st-utf8-target')));
    const invalidTargetMessage = expectFailure(
      () => appendTaskRun({ targetPath: invalidUtf8Target, pendingPath, lockPath }),
      'invalid UTF-8 target',
    );
    selfTestAssert(
      invalidTargetMessage.includes('target is not valid UTF-8'),
      'invalid UTF-8 target must fail explicitly',
    );

    // Failure: target itself is a symlink.
    const realTarget = path.join(dir, 'real-target.jsonl');
    fs.writeFileSync(realTarget, beforeBuffer);
    const symlinkTarget = path.join(dir, 'symlink-target.jsonl');
    fs.symlinkSync(realTarget, symlinkTarget);
    fs.writeFileSync(pendingPath, JSON.stringify(dummyTaskRun('st-7')));
    expectFailure(
      () => appendTaskRun({ targetPath: symlinkTarget, pendingPath, lockPath }),
      'symlinked target',
    );

    // Failure: any existing lock is rejected without inspection or removal.
    const existingLockDir = fs.mkdirSync(path.join(dir, 'existing-lock'), { recursive: true });
    const existingLockTarget = path.join(existingLockDir, 'task-runs.jsonl');
    const existingLockPending = path.join(existingLockDir, 'pending-task-run.json');
    const existingLockPath = path.join(existingLockDir, 'task-runs.jsonl.lock');
    fs.writeFileSync(existingLockTarget, beforeBuffer);
    fs.writeFileSync(existingLockPending, JSON.stringify(dummyTaskRun('st-existing-lock')));
    fs.writeFileSync(existingLockPath, 'opaque lock payload');
    const existingLockMessage = expectFailure(
      () => appendTaskRun({
        targetPath: existingLockTarget,
        pendingPath: existingLockPending,
        lockPath: existingLockPath,
      }),
      'append with existing lock',
    );
    selfTestAssert(
      existingLockMessage.includes(
        'lock exists; manual recovery required after confirming no append process is active',
      ),
      'existing lock refusal must explain manual recovery',
    );
    selfTestAssert(fs.existsSync(existingLockPath), 'existing lock must be preserved');
    selfTestAssert(fs.existsSync(existingLockPending), 'pending must survive existing-lock refusal');
    selfTestAssert(
      fs.readFileSync(existingLockTarget).equals(beforeBuffer),
      'existing-lock refusal must not touch target',
    );

    console.log('self-test PASS: strict v1.2 validation for all record types (valid+invalid fixtures),');
    console.log('prefix preserved, exact single-line append via one fd, strict full re-parse,');
    console.log('duplicate/newline/symlink/invalid-pending and invalid-UTF-8 rejections,');
    console.log('existing lock fail-closed/manual-recovery refusal; pending kept on failure');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

/* ---------------------------------- main ---------------------------------- */

function main() {
  const args = process.argv.slice(2);
  const modes = new Set(['--append', '--verify', '--self-test']);
  if (args.length !== 1 || !modes.has(args[0])) {
    console.error('usage: node .opencode/evals/append-task-run.mjs --append|--verify|--self-test');
    process.exit(2);
  }
  const mode = args[0];
  try {
    if (mode === '--append') {
      const result = appendTaskRun({
        targetPath: TARGET_PATH,
        pendingPath: PENDING_PATH,
        lockPath: LOCK_PATH,
      });
      console.log(
        `append OK: record_id=${result.recordId} (${result.bytes} bytes); ` +
          `task-runs.jsonl now has ${result.totalRecords} records; pending removed`,
      );
    } else if (mode === '--verify') {
      const state = verifyTarget(TARGET_PATH);
      console.log(
        `verify PASS: ${state.count} records; every nonempty line is valid JSON with a strict ` +
          'v1.2 envelope/payload and record ids are unique',
      );
    } else {
      runSelfTest();
      console.log('self-test ran in a temp dir only; real task-runs.jsonl and lock untouched');
    }
  } catch (err) {
    if (err instanceof OpError) {
      console.error(`${mode} failed: ${err.message}`);
      process.exit(1);
    }
    throw err;
  }
}

main();
