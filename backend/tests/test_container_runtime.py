"""M6-Q6 runtime6: the image runtime stage must ship Deno and yt-dlp-ejs.

yt-dlp needs a JS runtime (Deno >= 2.3.0) and the ``yt-dlp-ejs`` companion for
YouTube extraction. These tests only read the Dockerfile text; no Docker, real
package, binary or network is used. The metadata check snippet is executed
against a fake ``importlib.metadata.version``.
"""

import importlib.metadata
import re
import shlex
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"
DENO_COPY = "COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno"


def _stages(text: str) -> dict[str, str]:
    """Split Dockerfile text into ``{stage name: body}`` at ``FROM ... AS``."""
    joined = text.replace("\\\n", " ")
    stages: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in joined.splitlines():
        match = re.match(r"FROM\s+\S+\s+AS\s+(\S+)", line, re.IGNORECASE)
        if match:
            current = stages.setdefault(match.group(1), [])
        elif current is not None:
            current.append(line)
    return {name: "\n".join(lines) for name, lines in stages.items()}


@pytest.fixture(scope="module")
def runtime_stage() -> str:
    stages = _stages(DOCKERFILE.read_text(encoding="utf-8"))
    assert "runtime" in stages
    return stages["runtime"]


def test_stage_split_separates_frontend_node_stage(runtime_stage: str) -> None:
    stages = _stages(DOCKERFILE.read_text(encoding="utf-8"))
    assert "npm ci" in stages["frontend-build"]
    assert "npm ci" not in runtime_stage
    assert "deno" not in stages["frontend-build"].lower()


def test_runtime_copies_pinned_official_deno_binary_onto_path(
    runtime_stage: str,
) -> None:
    assert DENO_COPY in runtime_stage.splitlines()
    # /usr/local/bin is on the python:3.12-slim PATH.
    assert "bin-2.9.8" in runtime_stage
    assert "curl" not in runtime_stage
    assert "remote-components" not in runtime_stage


def test_runtime_installs_ytdlp_default_extra_quoted(runtime_stage: str) -> None:
    assert 'pip install --no-cache-dir "yt-dlp[default]"' in runtime_stage
    assert not re.search(r"pip install --no-cache-dir yt-dlp(\s|$)", runtime_stage)


def test_runtime_keeps_existing_ffmpeg_and_cmd(runtime_stage: str) -> None:
    assert "apt-get install -y --no-install-recommends ffmpeg" in runtime_stage
    assert (
        "alembic upgrade head && exec uvicorn app.main:app" in runtime_stage
    )
    assert "--timeout-graceful-shutdown 5" in runtime_stage


def _build_check_run(runtime_stage: str) -> str:
    runs = [
        line for line in runtime_stage.splitlines() if "deno --version" in line
    ]
    assert len(runs) == 1
    return runs[0]


def test_build_runs_version_checks_fail_fast(runtime_stage: str) -> None:
    run = _build_check_run(runtime_stage)
    assert run.lstrip().startswith("RUN deno --version")
    assert "&& yt-dlp --version" in run
    assert "&& python -c" in run
    # The checks run after both the Deno copy and the pip install.
    assert runtime_stage.index(DENO_COPY) < runtime_stage.index(run)
    assert runtime_stage.index('"yt-dlp[default]"') < runtime_stage.index(run)


def _metadata_snippet(runtime_stage: str) -> str:
    run = _build_check_run(runtime_stage)
    tokens = shlex.split(run.split("&& python -c", 1)[1])
    assert len(tokens) == 1
    return tokens[0]


def test_metadata_snippet_compiles_and_reads_ejs_version(
    runtime_stage: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    snippet = _metadata_snippet(runtime_stage)
    compile(snippet, "<dockerfile-snippet>", "exec")
    asked: list[str] = []

    def fake_version(name: str) -> str:
        asked.append(name)
        return "9.9.9"

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    exec(snippet, {})  # noqa: S102 - local snippet from the repo Dockerfile
    assert asked == ["yt-dlp-ejs"]
    assert "9.9.9" in capsys.readouterr().out


def test_metadata_snippet_fails_when_ejs_missing(
    runtime_stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    snippet = _metadata_snippet(runtime_stage)

    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(importlib.metadata.PackageNotFoundError):
        exec(snippet, {})  # noqa: S102
