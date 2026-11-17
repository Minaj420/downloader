"""Q0 / finding 18: orderly shutdown must not hang on an open SSE stream.

Uvicorn's HTTP graceful wait is unbounded by default, and the SSE stream never
completes on its own, so lifespan shutdown (``queue_manager.stop()`` and then
``engine.dispose()``) would never start while a browser stays connected. The
image CMD bounds that wait; the app lifecycle itself is unchanged.

The config tests guard the deployment files (no Docker is run here). The
process tests start a real local uvicorn subprocess on loopback with temporary
state/staging/library directories, hold an SSE connection open and send
SIGTERM, proving the flag bounds the wait and that without it the process stays
alive until the client disconnects.
"""

import http.client
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE_FILE = REPO_ROOT / "compose.yml"

GRACEFUL_FLAG = "--timeout-graceful-shutdown"
EXPECTED_GRACEFUL_SECONDS = 5
EXPECTED_STOP_GRACE_SECONDS = 30
# Room the outer stop budget must leave after the HTTP graceful wait for the
# queue drain/settlement and engine dispose (not a settlement guarantee).
MIN_SETTLEMENT_ROOM_SECONDS = 20

READY_TIMEOUT_SECONDS = 30.0
EXIT_MARGIN_SECONDS = 8.0
BASELINE_ALIVE_CHECK_SECONDS = 1.5
BASELINE_EXIT_TIMEOUT_SECONDS = 10.0
CLEANUP_WAIT_SECONDS = 10.0

TIMEOUT_LOG = "timeout graceful shutdown exceeded"
WAITING_LOG = "Waiting for connections to close"
LIFESPAN_DONE_LOG = "Application shutdown complete"
_RUNNING_RE = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+)")

posix_signals = pytest.mark.skipif(
    os.name != "posix" or not hasattr(signal, "SIGTERM"),
    reason="POSIX SIGTERM delivery is required",
)


# ---------------------------------------------------------------------------
# Deployment config parsing
# ---------------------------------------------------------------------------


def _runtime_stage_lines() -> list[str]:
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    from_indexes = [
        i for i, line in enumerate(lines) if line.strip().upper().startswith("FROM ")
    ]
    assert from_indexes, "Dockerfile has no FROM stage"
    return lines[from_indexes[-1]:]


def _runtime_cmd() -> list[str]:
    cmd_lines = [
        line.strip() for line in _runtime_stage_lines() if line.strip().upper().startswith("CMD ")
    ]
    assert cmd_lines, "runtime stage has no CMD"
    # Docker uses the last CMD of the final stage.
    raw = cmd_lines[-1][len("CMD "):].strip()
    parsed = json.loads(raw)  # Exec (JSON) form only; shell form would fail here.
    assert isinstance(parsed, list) and all(isinstance(part, str) for part in parsed)
    return parsed


def _uvicorn_args() -> list[str]:
    cmd = _runtime_cmd()
    assert cmd[:2] == ["sh", "-c"] and len(cmd) == 3, cmd
    tokens = shlex.split(cmd[2])
    prefix = ["alembic", "upgrade", "head", "&&", "exec", "uvicorn"]
    assert tokens[: len(prefix)] == prefix, tokens
    args = tokens[len(prefix):]
    # A single exec'd uvicorn: nothing may run after it or wrap it.
    for operator in ("&&", "||", ";", "|", "&"):
        assert operator not in args, args
    assert args and args[0] == "app.main:app", args
    return args


def _option_value(args: list[str], option: str) -> str:
    values: list[str] = []
    for index, arg in enumerate(args):
        if arg == option:
            assert index + 1 < len(args), f"{option} has no value"
            values.append(args[index + 1])
        elif arg.startswith(option + "="):
            values.append(arg.split("=", 1)[1])
    assert len(values) == 1, f"expected exactly one {option}, got {values}"
    return values[0]


def _graceful_seconds() -> int:
    raw = _option_value(_uvicorn_args(), GRACEFUL_FLAG)
    assert raw.isdigit(), f"{GRACEFUL_FLAG} must be a finite whole number, got {raw!r}"
    return int(raw)


def _compose_stop_grace_seconds(service: str = "downloader") -> int:
    lines = COMPOSE_FILE.read_text(encoding="utf-8").splitlines()
    in_services = False
    in_service = False
    service_indent = -1
    values: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_services = stripped == "services:"
            in_service = False
            continue
        if not in_services:
            continue
        if stripped.endswith(":") and (service_indent < 0 or indent <= service_indent):
            service_indent = indent
            in_service = stripped == f"{service}:"
            continue
        if in_service and indent > service_indent and stripped.startswith("stop_grace_period:"):
            values.append(stripped.split(":", 1)[1].strip().strip("'\""))
    assert len(values) == 1, f"expected one stop_grace_period for {service}, got {values}"
    match = re.fullmatch(r"(\d+)s", values[0])
    assert match, f"stop_grace_period must be whole seconds like '30s', got {values[0]!r}"
    return int(match.group(1))


def test_runtime_cmd_keeps_migrations_and_execs_uvicorn() -> None:
    args = _uvicorn_args()
    assert _option_value(args, "--host") == "0.0.0.0"
    assert _option_value(args, "--port") == "8080"


def test_dockerfile_has_no_entrypoint_wrapper() -> None:
    entrypoints = [
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.strip().upper().startswith("ENTRYPOINT")
    ]
    assert entrypoints == []


def test_runtime_cmd_bounds_http_graceful_wait() -> None:
    assert _graceful_seconds() == EXPECTED_GRACEFUL_SECONDS


def test_compose_stop_grace_leaves_room_after_http_wait() -> None:
    stop_grace = _compose_stop_grace_seconds()
    assert stop_grace == EXPECTED_STOP_GRACE_SECONDS
    assert stop_grace - _graceful_seconds() >= MIN_SETTLEMENT_ROOM_SECONDS


# ---------------------------------------------------------------------------
# Real local uvicorn subprocess (owned by the test, loopback only)
# ---------------------------------------------------------------------------


@dataclass
class _Server:
    process: subprocess.Popen[bytes]
    log_path: Path
    port: int

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")


def _child_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("DOWNLOADER_")}
    for name in ("state", "staging", "library"):
        directory = tmp_path / name
        directory.mkdir()
        env[f"DOWNLOADER_{name.upper()}_DIR"] = str(directory)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _wait_ready(server: _Server) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        assert server.process.poll() is None, f"server exited early:\n{server.log()}"
        match = _RUNNING_RE.search(server.log())
        if match:
            server.port = int(match.group(1))
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            try:
                conn.request("GET", "/api/v1/health")
                response = conn.getresponse()
                response.read()
                if response.status == 200:
                    assert server.process.poll() is None
                    return
            except OSError:
                pass
            finally:
                conn.close()
        time.sleep(0.05)
    raise AssertionError(f"server not ready within {READY_TIMEOUT_SECONDS}s:\n{server.log()}")


def _wait_log(server: _Server, needle: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in server.log():
            return
        time.sleep(0.05)
    raise AssertionError(f"log line {needle!r} not seen within {timeout}s:\n{server.log()}")


@pytest.fixture
def start_server(tmp_path: Path) -> Iterator[Callable[[list[str]], _Server]]:
    started: list[tuple[subprocess.Popen[bytes], IO[bytes]]] = []

    def start(extra_args: list[str]) -> _Server:
        cwd = tmp_path / "cwd"  # No repository .env is visible from here.
        cwd.mkdir()
        log_path = tmp_path / "uvicorn.log"
        log_file = log_path.open("wb")
        args = [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--app-dir", str(BACKEND_DIR),
            "--host", "127.0.0.1", "--port", "0",
            *extra_args,
        ]
        try:
            process = subprocess.Popen(
                args, cwd=cwd, env=_child_env(tmp_path),
                stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                shell=False,
            )
        except BaseException:
            log_file.close()
            raise
        started.append((process, log_file))
        server = _Server(process=process, log_path=log_path, port=0)
        _wait_ready(server)
        return server

    try:
        yield start
    finally:
        for process, log_file in started:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=CLEANUP_WAIT_SECONDS)
            finally:
                log_file.close()


def _open_sse(port: int) -> http.client.HTTPConnection:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/api/v1/events", headers={"Accept": "text/event-stream"})
        response = conn.getresponse()  # Status line + headers only; body stays open.
        assert response.status == 200
        assert response.getheader("content-type", "").startswith("text/event-stream")
    except BaseException:
        conn.close()
        raise
    return conn


@posix_signals
def test_sigterm_with_open_sse_is_bounded_by_graceful_flag(
    start_server: Callable[[list[str]], _Server],
) -> None:
    graceful = _graceful_seconds()  # Taken from the real Dockerfile CMD.
    server = start_server([GRACEFUL_FLAG, str(graceful)])
    sse = _open_sse(server.port)
    try:
        server.process.send_signal(signal.SIGTERM)
        returncode = server.process.wait(timeout=graceful + EXIT_MARGIN_SECONDS)
    finally:
        sse.close()
    log = server.log()
    assert returncode in (0, -signal.SIGTERM), log
    assert WAITING_LOG in log, log
    assert TIMEOUT_LOG in log, log
    # The lifespan finally (queue_manager.stop then engine.dispose) ran.
    assert LIFESPAN_DONE_LOG in log, log
    assert log.index(TIMEOUT_LOG) < log.index(LIFESPAN_DONE_LOG)


@posix_signals
def test_sigterm_without_flag_waits_for_sse_until_client_disconnects(
    start_server: Callable[[list[str]], _Server],
) -> None:
    server = start_server([])
    sse = _open_sse(server.port)
    try:
        server.process.send_signal(signal.SIGTERM)
        _wait_log(server, WAITING_LOG, timeout=5)
        with pytest.raises(subprocess.TimeoutExpired):
            server.process.wait(timeout=BASELINE_ALIVE_CHECK_SECONDS)
        assert LIFESPAN_DONE_LOG not in server.log()
    finally:
        sse.close()
    returncode = server.process.wait(timeout=BASELINE_EXIT_TIMEOUT_SECONDS)
    log = server.log()
    assert returncode in (0, -signal.SIGTERM), log
    assert TIMEOUT_LOG not in log, log
    assert LIFESPAN_DONE_LOG in log, log
