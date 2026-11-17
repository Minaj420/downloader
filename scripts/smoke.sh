#!/usr/bin/env bash
# Usage: sudo bash scripts/smoke.sh network|restart <URL>   (see README "Smoke script")
# Exit: 0 PASS, 1 FAIL, 2 BLOCKED, 64 bad CLI. Never prints the URL, API bodies or docker logs.
set -u -o pipefail
export LC_ALL=C.UTF-8
CT=downloader
RC=1 MSG="unexpected exit" NET="" MUTATED=0 PHASE=prep
SLEEP=${SMOKE_SLEEP:-1} PROG_T=${SMOKE_PROGRESS_TIMEOUT:-120} FAIL_T=${SMOKE_FAIL_TIMEOUT:-180}
[[ $SLEEP =~ ^[0-9]+(\.[0-9]+)?$ && $PROG_T$FAIL_T =~ ^[0-9]+$ ]] || exit 64
src=${BASH_SOURCE[0]}; [[ $src == */* ]] || src=./$src
ROOT=$(cd "${src%/*}/.." && pwd) || exit 64
MODE=${1-} URL=${2-}
if [[ $# -ne 2 || ! $MODE =~ ^(network|restart)$ || ${#URL} -gt 2048 \
      || ! $URL =~ ^https?://[^[:space:]]+$ ]]; then
  echo "usage: smoke.sh network|restart <URL>"; exit 64
fi

PY='
import json, sys, urllib.request as u
def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = u.Request("http://127.0.0.1:8080/api/v1" + path, data=data, method=method,
                    headers={"Content-Type": "application/json"})
    with u.urlopen(req, timeout=10) as r:
        return json.load(r)
try:
    op = sys.argv[1]
    if op == "ready":
        call("GET", "/health")
    elif op == "idle":
        # GET /jobs only returns the latest 50 rows: read the DB read-only instead.
        import sqlite3
        from app.config import get_settings
        path = get_settings().state_dir / "downloader.db"
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        db.execute("PRAGMA query_only=1")
        busy = db.execute(
            "SELECT EXISTS(SELECT 1 FROM download_jobs"
            " WHERE status NOT IN (\x27completed\x27,\x27failed\x27,\x27cancelled\x27))"
            " OR EXISTS(SELECT 1 FROM job_items WHERE status IN"
            " (\x27analyzing\x27,\x27downloading\x27,\x27post_processing\x27,\x27finalizing\x27))"
        ).fetchone()[0]
        sys.exit(3 if busy else 0)
    elif op == "start":
        a = call("POST", "/analysis", {"url": sys.stdin.read().strip()})
        fmts = a.get("formats") or []
        if a.get("content_type") != "single" or not fmts:
            sys.exit(4)
        pick = next((f for f in fmts if f["kind"] == "audio_only"), fmts[0])
        job = call("POST", "/jobs", {"analysis_id": a["analysis_id"],
                                     "variant_id": pick["variant_id"], "output_profile": "original"})
        print(job["id"])
    elif op == "status":
        j = call("GET", "/jobs/" + sys.argv[2])
        e = j.get("error") or {}
        b = (j.get("progress") or {}).get("downloaded_bytes") or 0
        print(j["status"], b, e.get("code") or "-", str(bool(e.get("retryable"))).lower())
except SystemExit:
    raise
except Exception:
    sys.exit(1)
'
api() { timeout 40 docker exec -i "$CT" python -c "$PY" "$@" 2>/dev/null; }
dk() { local t=$1; shift; timeout "$t" docker "$@" 2>/dev/null; }
dc() {
  local t=$1; shift
  timeout "$t" docker compose --project-directory "$ROOT" -f "$ROOT/compose.yml" "$@" >/dev/null 2>&1
}
fail() { MSG=$1; RC=1; exit 1; }
blocked() { MSG=$1; RC=2; exit 2; }
wait_ready() {
  local end=$((SECONDS + ${1:-90}))
  while ! api ready </dev/null >/dev/null; do
    (( SECONDS < end )) || return 1
    sleep "$SLEEP"
  done
}
net_connected() {
  local out
  out=$(dk 30 inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "$CT") \
    && grep -qxF -- "$NET" <<<"$out"
}
recover() {  # idempotent; only verified state counts
  if [[ -n $NET ]] && ! net_connected; then
    dk 30 network connect --alias "$CT" "$NET" "$CT" >/dev/null
    net_connected || return 1
  fi
  if (( MUTATED )); then
    [[ $(dk 30 inspect -f '{{.State.Running}}' "$CT") == true ]] || dk 60 start "$CT" >/dev/null
    wait_ready 90 || return 1
  fi
}
cleanup() {
  trap '' INT TERM
  trap - EXIT
  if ! recover; then
    RC=1 MSG="RECOVERY FAILED: restore network/container manually"
  fi
  if (( RC == 0 )); then echo "PASS: $MODE"; else
    case $RC in 2) echo "BLOCKED: $MSG";; *) echo "FAIL: $MSG [phase=$PHASE]";; esac
  fi
  exit "$RC"
}
trap cleanup EXIT
trap 'MSG="interrupted"; RC=1; exit 1' INT TERM

# status JOB -> sets S (status) B (bytes) C (error code) R (retryable)
job_status() {
  local out
  out=$(api status "$1" </dev/null) || return 1
  read -r S B C R <<<"$out"
  [[ $B =~ ^[0-9]+$ && -n ${S-} ]]
}
wait_terminal() {  # until failed; any other terminal state or timeout is FAIL
  local end=$((SECONDS + FAIL_T))
  while :; do
    job_status "$1" || fail "job polling failed"
    case $S in
      failed) return 0;;
      completed) blocked "job completed before the interruption took effect";;
      cancelled) fail "job was cancelled instead of failing";;
    esac
    (( SECONDS < end )) || fail "timeout waiting for failure"
    sleep "$SLEEP"
  done
}
wait_progress() {  # intervene only on real byte growth while downloading
  local end=$((SECONDS + PROG_T)) prev=-1
  while :; do
    job_status "$1" || fail "job polling failed"
    case $S in
      downloading) (( prev >= 0 && B > prev )) && return 0; prev=$B;;
      queued|analyzing) ;;
      *) blocked "job left downloading before intervention (too fast or not downloadable)";;
    esac
    (( SECONDS < end )) || fail "timeout waiting for download progress"
    sleep "$SLEEP"
  done
}
check_failure_log() {
  local logs line detail="" n=0 after=0 part m rest
  local -A seen=()
  local start='^((INFO|WARNING|DEBUG|CRITICAL|ERROR):( {3,}|[A-Za-z_][A-Za-z0-9_.]*:)|yt-dlp |[0-9]{4}-[0-9]{2}-)'
  logs=$(timeout 30 docker logs --tail 5000 "$CT" 2>&1) || fail "docker logs failed"
  while IFS= read -r line; do
    if (( after )); then
      after=0
      [[ $line =~ $start ]] || fail "failure log has a continuation line"
    fi
    if [[ $line == *"yt-dlp job failed: job_id=$1 kind="* ]]; then
      n=$((n + 1)); after=1; detail=${line#*" detail="}
    fi
  done <<<"$logs"
  (( n == 1 )) || fail "expected exactly one failure log for the job"
  [[ -n $detail && $detail != "<none>" ]] || fail "failure log detail empty"
  [[ $detail != *$'\e'* && $detail != *$'\r'* ]] || fail "failure detail has ANSI/CR"
  (( ${#detail} <= 500 )) || fail "failure detail longer than 500 characters"
  rest=${detail//ERROR:/$'\x01'}
  IFS=$'\x01' read -ra parts <<<"$rest"
  for part in "${parts[@]}"; do
    [[ $part =~ ^[[:space:]]*(.*[^[:space:]])[[:space:]]*$ ]] || continue
    m=${BASH_REMATCH[1]}
    [[ -z ${seen[$m]+x} ]] || fail "failure detail repeats an ERROR message"
    seen[$m]=1
  done
}

[[ -f $ROOT/compose.yml ]] || blocked "compose.yml not found next to scripts/"
api ready </dev/null >/dev/null || blocked "API not reachable in the downloader container"
api idle </dev/null >/dev/null || blocked "not idle (other jobs queued/active) or idle check failed"
PHASE=build
dc 1800 build downloader || fail "image build failed (nothing interrupted)"
PHASE=up; MUTATED=1
dc 300 up -d --no-deps downloader || fail "compose up failed"
wait_ready 90 || fail "API not ready after up"
api idle </dev/null >/dev/null || blocked "not idle (other jobs queued/active) or idle check failed"
if [[ $MODE == network ]]; then
  nets=$(dk 30 inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "$CT") \
    || fail "network inspect failed"
  [[ $nets =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ && $nets != host && $nets != none ]] \
    || blocked "need exactly one regular connected network"
fi
PHASE=job
JOB=$(printf '%s' "$URL" | api start); rc=$?
(( rc == 4 )) && blocked "URL is not a single downloadable item"
(( rc == 0 )) && [[ $JOB =~ ^[A-Za-z0-9_-]+$ ]] || fail "could not start job via API"
wait_progress "$JOB"
PHASE=intervene
if [[ $MODE == network ]]; then
  NET=$nets   # recovery intent is recorded before the mutation
  dk 30 network disconnect "$NET" "$CT" >/dev/null || fail "network disconnect failed"
  wait_terminal "$JOB"
  [[ $C == network_error && $R == true ]] || fail "job error is not retryable network_error"
else
  STOP_TS=$(date +%s)
  dc 90 stop || fail "compose stop failed"   # compose.yml stop_grace_period (30s) applies
  [[ $(dk 30 inspect -f '{{.State.ExitCode}} {{.State.OOMKilled}}' "$CT") == "0 false" ]] \
    || fail "container did not exit cleanly (ExitCode 0, no OOM)"
  logs=$(timeout 30 docker logs --since "$STOP_TS" "$CT" 2>&1) || fail "docker logs failed"
  [[ ${logs,,} == *"shutdown complete"* ]] || fail "no shutdown complete in logs"
  dc 120 start downloader || fail "compose start failed"
  wait_ready 90 || fail "API not ready after start"
  wait_terminal "$JOB"
  [[ $C == interrupted_by_restart && $R == true ]] || fail "job is not retryable interrupted_by_restart"
fi
PHASE=logs
[[ $MODE == network ]] && check_failure_log "$JOB"
PHASE=recover
RC=0 MSG=ok
