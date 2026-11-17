"""Tests for scripts/smoke.sh using only a fake ``docker`` (never the real one).

Real Docker is unreachable by construction: PATH holds only a temp dir with the fake
``docker`` plus a handful of symlinked coreutils, and DOCKER_HOST points nowhere.
"""

import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke.sh"
BASH = shutil.which("bash") or "/bin/bash"
URL = "https://secret.example/watch?v=SECRETTOKEN123"
TOOLS = ("bash", "timeout", "sleep", "date", "grep")

FAKE_DOCKER = r"""#!@BASH@
D=$FAKE_DIR; S=${FAKE_SCENARIO:-ok}
get() { if [[ -f $D/$1 ]]; then printf '%s' "$(<"$D/$1")"; else printf '%s' "${2-}"; fi; }
put() { printf '%s' "$2" > "$D/$1"; }
printf '%s\n' "${*//$'\n'/ }" >> "$D/argv"
case $1 in
compose)
  shift; while [[ $1 == --project-directory || $1 == -f ]]; do shift 2; done
  printf 'compose %s\n' "$*" >> "$D/calls"
  case $1 in
    build) [[ $S == build_fail ]] && exit 1;;
    stop) put down 1; [[ $S == stop_fail ]] && exit 1;;
    start) put down 0; put restarted 1;;
  esac
  exit 0;;
exec)
  printf 'exec:%s\n' "${7-}" >> "$D/calls"
  [[ $(get down 0) == 1 ]] && exit 1
  case $7 in
    ready) [[ $S == noapi ]] && exit 1; exit 0;;
    idle)
      if [[ -n ${FAKE_REALIDLE-} ]]; then  # run the real in-container idle code on a temp DB
        cd "$FAKE_BACKEND" && DOWNLOADER_STATE_DIR=$D "$FAKE_PYTHON" -c "$6" idle
        exit $?
      fi
      [[ $S == busy ]] && exit 3; exit 0;;
    start) read -r url; put stdin_seen "$url"; [[ $S == multi ]] && exit 4
           echo job1; exit 0;;
    status)
      n=$(( $(get n 0) + 1 )); put n $n
      [[ $S == poll_error ]] && exit 1
      # race: job finishes right after the second progress sample, before the mutation
      [[ $S == race* ]] && (( n >= 3 )) && { echo "completed 500 - false"; exit 0; }
      if [[ $(get disconnected 0) == 1 ]]; then
        case $S in
          hang) echo "downloading $((n * 100)) - false";;
          wrongcode) echo "failed 9 extractor_failed false";;
          *) echo "failed 9 network_error true";;
        esac
        exit 0
      fi
      [[ $(get restarted 0) == 1 ]] && { echo "failed 9 interrupted_by_restart true"; exit 0; }
      case $S in
        stuck) echo "downloading 0 - false";;
        early) (( n >= 2 )) && echo "completed 500 - false" || echo "downloading 0 - false";;
        postproc) (( n >= 2 )) && echo "post_processing 5 - false" || echo "downloading 0 - false";;
        *) echo "downloading $((n * 100)) - false";;
      esac
      exit 0;;
  esac
  exit 1;;
inspect)
  case $3 in
    *Networks*)
      [[ $S == twonets ]] && { echo dl_default; echo other; exit 0; }
      [[ $(get disconnected 0) == 1 ]] || echo dl_default;;
    *ExitCode*) if [[ $S == oom ]]; then echo "137 true"; else echo "0 false"; fi;;
    *Running*) if [[ $(get down 0) == 1 ]]; then echo false; else echo true; fi;;
  esac
  exit 0;;
network)
  printf 'network %s\n' "$2" >> "$D/calls"
  case $2 in
    disconnect)
      [[ $S == disc_fail ]] && exit 1
      put disconnected 1
      [[ $S == disc_then_fail ]] && exit 1
      exit 0;;
    connect)
      [[ $S == *connect_fail ]] && exit 1
      [[ $(get disconnected 0) == 0 ]] && exit 1
      put disconnected 0; put reconnects $(( $(get reconnects 0) + 1 )); exit 0;;
  esac
  exit 1;;
start) printf 'start\n' >> "$D/calls"; put down 0; exit 0;;
logs)
  if [[ $* == *--since* ]]; then
    [[ $S == noshutdown ]] || echo "INFO:     Application shutdown complete." >&2
    exit 0
  fi
  d="detail=ERROR: unable to download"
  case $S in
    repeated) d="detail=ERROR: boom ERROR: boom";;
    long501) d="detail=$(printf 'é%.0s' {1..501})";;
    log500) d="detail=$(printf 'é%.0s' {1..500})";;
    ansi) d=$'detail=\e[31mERROR: boom\e[0m';;
    cr) d=$'detail=ERROR: boom\r';;
  esac
  printf '%s\n' "INFO:     Started server process" >&2
  [[ $S == nolog ]] || printf '%s\n' "yt-dlp job failed: job_id=job1 kind=network_error $d" >&2
  [[ $S == double ]] && printf '%s\n' "yt-dlp job failed: job_id=job1 kind=network_error $d" >&2
  [[ $S == multiline ]] && printf '%s\n' "ERROR: unable to download" >&2
  printf '%s\n' 'INFO:     127.0.0.1:1 - "GET / HTTP/1.1" 200 OK' >&2
  exit 0;;
esac
exit 1
"""


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.dir = root / "state"
        self.dir.mkdir()
        bindir = root / "bin"
        bindir.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            assert found, tool
            (bindir / tool).symlink_to(found)
        docker = bindir / "docker"
        docker.write_text(FAKE_DOCKER.replace("@BASH@", BASH))
        docker.chmod(0o755)
        # Real Docker must be unreachable: the only `docker` on PATH is the fake.
        assert shutil.which("docker", path=str(bindir)) == str(docker)
        self.bindir = bindir
        self.root = root

    def env(self, scenario: str, **extra: str) -> dict[str, str]:
        env = {
            "PATH": str(self.bindir),
            "HOME": str(self.root),
            "DOCKER_HOST": "unix:///nonexistent/docker.sock",
            "FAKE_DIR": str(self.dir),
            "FAKE_SCENARIO": scenario,
            "SMOKE_SLEEP": "0.05",
            "SMOKE_PROGRESS_TIMEOUT": "5",
            "SMOKE_FAIL_TIMEOUT": "5",
        }
        env.update(extra)
        return env

    def read(self, name: str) -> str:
        path = self.dir / name
        return path.read_text() if path.exists() else ""

    def calls(self) -> list[str]:
        return self.read("calls").splitlines()

    def assert_quiet(self, out: str) -> None:
        for secret in ("SECRETTOKEN123", "secret.example", "unable to download", "boom"):
            assert secret not in out
        assert "SECRETTOKEN123" not in self.read("argv")  # URL is never in docker argv

    def run(
        self, mode: str, scenario: str = "ok", **extra: str
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [BASH, str(SCRIPT), mode, URL],
            env=self.env(scenario, **extra),
            capture_output=True,
            text=True,
            timeout=30,
            cwd=self.root,
        )
        self.assert_quiet(proc.stdout + proc.stderr)
        return proc


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def test_bash_syntax() -> None:
    proc = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "argv",
    [[], ["network"], ["bogus", URL], ["network", "ftp://x/y"], ["network", URL, "x"],
     ["restart", "http://a b"]],
)
def test_invalid_argv_touches_nothing(box: Sandbox, argv: list[str]) -> None:
    proc = subprocess.run(
        [BASH, str(SCRIPT), *argv], env=box.env("ok"), capture_output=True, text=True,
        timeout=10, cwd=box.root,
    )
    assert proc.returncode == 64
    assert box.calls() == []
    assert "SECRET" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("scenario", ["ok", "log500"])
def test_network_pass(box: Sandbox, scenario: str) -> None:
    proc = box.run("network", scenario)
    assert proc.returncode == 0, proc.stdout
    assert "PASS" in proc.stdout
    calls = box.calls()
    up = calls.index("compose up -d --no-deps downloader")
    assert calls.index("compose build downloader") < up < calls.index("network disconnect")
    assert box.read("disconnected") == "0"  # reconnected and verified
    assert box.read("stdin_seen") == URL  # URL travelled as data on stdin
    assert str(SCRIPT.parents[1] / "compose.yml") in box.read("argv")  # root from script path


def test_restart_pass(box: Sandbox) -> None:
    proc = box.run("restart")
    assert proc.returncode == 0, proc.stdout
    calls = box.calls()
    assert "compose stop" in calls and "compose start downloader" in calls
    assert "exec:retry" not in calls
    subcommands = {c.split()[1] for c in calls if c.startswith("compose ")}
    assert not subcommands & {"down", "rm", "kill", "restart", "prune"}


@pytest.mark.parametrize("mode", ["network", "restart"])
@pytest.mark.parametrize("scenario", ["early", "postproc"])
def test_early_terminal_is_blocked(box: Sandbox, mode: str, scenario: str) -> None:
    proc = box.run(mode, scenario)
    assert proc.returncode == 2 and "BLOCKED" in proc.stdout and "PASS" not in proc.stdout
    assert "network disconnect" not in box.calls() and "compose stop" not in box.calls()


@pytest.mark.parametrize("scenario", ["multi", "busy", "noapi", "twonets"])
def test_preflight_blocked_without_cutting(box: Sandbox, scenario: str) -> None:
    proc = box.run("network", scenario)
    assert proc.returncode == 2 and "BLOCKED" in proc.stdout
    assert "network disconnect" not in box.calls()


def test_build_failure_does_not_interrupt(box: Sandbox) -> None:
    proc = box.run("network", "build_fail")
    assert proc.returncode == 1 and "PASS" not in proc.stdout
    assert not any(c.startswith(("compose up", "network")) for c in box.calls())


@pytest.mark.parametrize(
    ("scenario", "extra"),
    [("poll_error", {}), ("stuck", {"SMOKE_PROGRESS_TIMEOUT": "1"}),
     ("hang", {"SMOKE_FAIL_TIMEOUT": "1"}), ("wrongcode", {})],
)
def test_polling_failures_fail_and_recover(
    box: Sandbox, scenario: str, extra: dict[str, str]
) -> None:
    proc = box.run("network", scenario, **extra)
    assert proc.returncode == 1 and "PASS" not in proc.stdout
    assert box.read("disconnected") in ("", "0")


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_signal_cleanup_reconnects(box: Sandbox, sig: signal.Signals) -> None:
    proc = subprocess.Popen(
        [BASH, str(SCRIPT), "network", URL], env=box.env("hang", SMOKE_FAIL_TIMEOUT="30"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=box.root,
    )
    try:
        deadline = time.monotonic() + 15
        while box.read("disconnected") != "1":
            assert time.monotonic() < deadline and proc.poll() is None
            time.sleep(0.05)
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 1 and "PASS" not in out
    assert box.read("disconnected") == "0"
    box.assert_quiet(out + err)


@pytest.mark.parametrize(
    ("mode", "scenario", "check"),
    [("network", "disc_fail", lambda b: b.read("disconnected") in ("", "0")),
     ("network", "disc_then_fail", lambda b: b.read("reconnects") == "1"),
     ("restart", "stop_fail", lambda b: b.read("down") == "0")],
)
def test_mutation_error_still_recovers(
    box: Sandbox, mode: str, scenario: str, check: object
) -> None:
    proc = box.run(mode, scenario)
    assert proc.returncode == 1 and "PASS" not in proc.stdout
    assert callable(check) and check(box)


def test_recovery_failure_is_never_pass(box: Sandbox) -> None:
    proc = box.run("network", "connect_fail")
    assert proc.returncode == 1 and "RECOVERY FAILED" in proc.stdout
    assert "PASS" not in proc.stdout


@pytest.mark.parametrize("mode", ["network", "restart"])
def test_completion_race_at_mutation_boundary_is_blocked(box: Sandbox, mode: str) -> None:
    proc = box.run(mode, "race")
    assert proc.returncode == 2 and "BLOCKED" in proc.stdout and "PASS" not in proc.stdout
    assert box.read("disconnected") in ("", "0") and box.read("down") in ("", "0")


def test_race_with_recovery_failure_is_fail(box: Sandbox) -> None:
    proc = box.run("network", "race_connect_fail")
    assert proc.returncode == 1 and "RECOVERY FAILED" in proc.stdout


def _real_idle_env(box: Sandbox) -> dict[str, str]:
    return {
        "FAKE_REALIDLE": "1",
        "FAKE_PYTHON": sys.executable,
        "FAKE_BACKEND": str(Path(__file__).resolve().parents[1]),
    }


def _make_db(box: Sandbox, old_status: str | None, item_status: str | None = None) -> None:
    db = sqlite3.connect(box.dir / "downloader.db")
    db.execute("CREATE TABLE download_jobs (id TEXT, status TEXT, created_at TEXT)")
    db.execute("CREATE TABLE job_items (id TEXT, job_id TEXT, status TEXT)")
    if old_status:  # oldest row, behind more than 50 newer terminal rows
        db.execute("INSERT INTO download_jobs VALUES ('old', ?, '2000-01-01')", (old_status,))
    for i in range(60):
        db.execute("INSERT INTO download_jobs VALUES (?, 'completed', '2030-01-01')", (f"j{i}",))
    if item_status:
        db.execute("INSERT INTO job_items VALUES ('c1', 'j1', ?)", (item_status,))
    db.commit()
    db.close()


@pytest.mark.parametrize(
    ("old_status", "item_status"),
    [("queued", None), ("downloading", None), ("post_processing", None), (None, "downloading")],
)
def test_real_idle_check_sees_old_nonterminal_work(
    box: Sandbox, old_status: str | None, item_status: str | None
) -> None:
    _make_db(box, old_status, item_status)
    proc = box.run("network", "ok", **_real_idle_env(box))
    assert proc.returncode == 2 and "BLOCKED" in proc.stdout
    calls = box.calls()
    assert not any(c.startswith(("compose build", "compose up", "compose stop", "network"))
                   for c in calls)


def test_real_idle_check_passes_when_all_terminal(box: Sandbox) -> None:
    _make_db(box, "failed", "queued")  # failed parent + queued retry-eligible child is idle
    proc = box.run("network", "ok", **_real_idle_env(box))
    assert proc.returncode == 0, proc.stdout


def test_real_idle_check_fails_closed_without_db(box: Sandbox) -> None:
    proc = box.run("network", "ok", **_real_idle_env(box))
    assert proc.returncode == 2 and "BLOCKED" in proc.stdout
    assert not any(c.startswith("compose build") for c in box.calls())


@pytest.mark.parametrize("scenario", ["oom", "noshutdown"])
def test_restart_checks_clean_shutdown(box: Sandbox, scenario: str) -> None:
    proc = box.run("restart", scenario)
    assert proc.returncode == 1 and "PASS" not in proc.stdout


@pytest.mark.parametrize(
    "scenario", ["multiline", "repeated", "long501", "ansi", "cr", "double", "nolog"]
)
def test_bad_failure_log_is_fail(box: Sandbox, scenario: str) -> None:
    proc = box.run("network", scenario)
    assert proc.returncode == 1 and "PASS" not in proc.stdout
    assert box.read("disconnected") == "0"
