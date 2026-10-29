"""Adapter protocol tests using a fake subprocess; never invoke Claude or change modes."""

import importlib.util
import io
import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def events():
    return [
        {"type": "system", "subtype": "init", "model": "claude-opus-fake", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Öneri ve gerekçe"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}},
        ]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "permission_denials": [], "result": "must-fix yok", "session_id": "s1"},
    ]


@pytest.fixture
def bridge(monkeypatch):
    module = load("claude-agent")
    monkeypatch.setattr(module, "git_context", lambda: "fixed read-only snapshot")
    calls = []

    class Input(io.StringIO):
        def close(self):
            self.saved = self.getvalue()
            super().close()

    class Child:
        def __init__(self, argv, **kwargs):
            self.argv, self.kwargs = argv, kwargs
            self.stdin = Input()
            self.stdout = io.StringIO("".join(json.dumps(e) + "\n" for e in module.events))
            self.returncode = module.exit_code
            calls.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    module.events, module.exit_code, module.calls = events(), 0, calls
    monkeypatch.setattr(module.subprocess, "Popen", Child)
    return module


def test_argv_stdin_and_inherited_settings_isolation(bridge, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-inherited")
    monkeypatch.setenv("ANTHROPIC_MODEL", "sonnet")
    monkeypatch.setenv("CLAUDECODE", "parent")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/custom/abonelik-ayarları")
    prompt = 'literal $(touch /nope); " --tools Bash'
    assert bridge.run("review", prompt) == 0
    child = bridge.calls[0]
    argv, env = child.argv, child.kwargs["env"]
    assert argv[:4] == ["claude", "-p", "--model", "opus"]
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert {"--strict-mcp-config", "--disable-slash-commands", "--no-chrome"} <= set(argv)
    assert "--bare" not in argv and "--dangerously-skip-permissions" not in argv
    assert prompt not in argv and prompt in child.stdin.saved
    assert "fixed read-only snapshot" in child.stdin.saved
    assert not child.kwargs.get("shell") and child.kwargs["cwd"] == bridge.ROOT
    assert child.kwargs["encoding"] == "utf-8"
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["disableAllHooks"] and not settings["enabledPlugins"]
    assert settings["permissions"]["allow"] == ["Read", "Grep", "Glob"]
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_MODEL" not in env
    assert "CLAUDECODE" not in env and env["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription"
    assert env["CLAUDE_CODE_SAFE_MODE"] == "1"
    assert env["CLAUDE_CONFIG_DIR"] == "/custom/abonelik-ayarları"
    report = json.loads(capsys.readouterr().out)
    assert report["observed_model"] == "claude-opus-fake" and report["session_id"] == "s1"
    assert report["result"] == "must-fix yok" and report["exit_code"] == 0


@pytest.mark.parametrize("role,model", [
    ("consult", "opus"), ("review", "opus"), ("implement", "sonnet"), ("implement", "opus"),
])
def test_resume_sends_current_role_settings_to_cli(bridge, role, model):
    # Fake argv coverage only; this does not prove live cross-role resume isolation.
    bridge.events[0]["model"] = f"claude-{model}-fake"
    assert bridge.run(role, "Scope and targeted commands decided by lead",
                      resume="session-id", model=model) == 0
    argv = bridge.calls[0].argv
    assert argv[argv.index("--model") + 1] == model
    assert argv[-2:] == ["--resume", "session-id"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    allow = settings["permissions"]["allow"]
    tools = argv[argv.index("--tools") + 1].split(",")
    if role == "implement":
        assert {"Edit", "Write", "Bash"} <= set(tools)
        assert "Edit" in allow and "Write" in allow and "Bash" not in allow
        assert "Bash(.venv/bin/pytest *)" in allow
        assert "Bash(.venv/bin/ruff check *)" in allow
        assert "Bash(.venv/bin/mypy *)" in allow and "Bash(npm test)" in allow
        assert "Edit(./STATE.md)" in settings["permissions"]["deny"]
    else:
        assert tools == ["Read", "Grep", "Glob"]
        assert not any(x.startswith("Bash") or x in {"Edit", "Write"} for x in allow)
        assert "Bash,Edit,Write" in argv[argv.index("--disallowedTools") + 1]
    assert not any("git " in x or "opencode" in x or "claude " in x for x in allow)


@pytest.mark.parametrize("failure", ["exit", "missing", "is_error", "denial", "deny-event",
                                     "malformed", "empty"])
def test_failed_or_incomplete_call_is_not_success(bridge, capsys, failure):
    if failure == "exit":
        bridge.exit_code = 7
    elif failure == "missing":
        bridge.events.pop()
    elif failure == "is_error":
        bridge.events[-1]["is_error"] = True
    elif failure == "denial":
        bridge.events[-1]["permission_denials"] = [{"tool_name": "Write"}]
    elif failure == "deny-event":
        bridge.events.insert(1, {"type": "system", "subtype": "permission_denied"})
    elif failure == "malformed":
        bridge.events.insert(1, ["not an event"])
    elif failure == "empty":
        bridge.events[-1]["result"] = ""
    assert bridge.run("consult", "advice") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["success"] is False
    if failure == "denial":
        assert report["permission_denials"] is True
        assert report["permission_denial_details"] == [{"tool_name": "Write"}]
    elif failure == "deny-event":
        assert report["permission_denials"] is True
        assert report["permission_denial_details"] == [
            {"type": "system", "subtype": "permission_denied"}
        ]
    assert len(bridge.calls) == 1  # No adapter retry or fallback.


def test_plain_stdin_and_optional_log(bridge, monkeypatch, tmp_path, capsys):
    bridge.events[0]["model"] = "claude-sonnet-fake"
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.setattr(bridge, "LOGS", tmp_path / "tmp" / "claude")
    monkeypatch.setattr(sys, "argv", [
        "bridge", "implement", "--log", "--resume", "s1", "--model", "sonnet",
    ])
    monkeypatch.setattr(sys, "stdin", io.StringIO("Allowed files: example.py; targeted tests ..."))
    assert bridge.main() == 0
    logs = list(bridge.LOGS.glob("*.jsonl"))
    assert len(logs) == 1 and json.loads(logs[0].read_text().splitlines()[-1]) == events()[-1]
    assert [p.name for p in bridge.LOGS.iterdir()] == [logs[0].name]
    assert "Allowed files: example.py" in bridge.calls[0].stdin.saved
    output = capsys.readouterr().out
    assert f"WATCH: bash scripts/claude-watch.sh tmp/claude/{logs[0].name}" in output


@pytest.mark.parametrize("argv", [["unknown", "prompt"],
                                 ["implement", "a", "extra"]])
def test_unknown_role_or_arg_does_not_invoke_claude(bridge, monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["bridge", *argv])
    with pytest.raises(SystemExit):
        bridge.main()
    assert not bridge.calls


@pytest.mark.parametrize("role,model,expected", [
    ("implement", None, "sonnet"), ("implement", "opus", "opus"),
    ("consult", None, "opus"), ("review", None, "opus"),
    ("consult", "opus", "opus"), ("review", "opus", "opus"),
])
def test_cli_model_selection_and_report(bridge, monkeypatch, capsys, role, model, expected):
    argv = ["bridge", role, "Scoped task"]
    if model:
        argv += ["--model", model]
    monkeypatch.setattr(sys, "argv", argv)
    bridge.events[0]["model"] = f"claude-{expected}-fake"
    assert bridge.main() == 0
    assert bridge.calls[0].argv[:4] == ["claude", "-p", "--model", expected]
    report = json.loads(capsys.readouterr().out)
    assert report["requested_model"] == expected
    assert report["observed_model"] == f"claude-{expected}-fake"
    assert report["success"] is True


@pytest.mark.parametrize("argv,error", [
    (["consult", "task", "--model", "sonnet"], "consult requires --model opus"),
    (["review", "task", "--model", "sonnet"], "review requires --model opus"),
    (["implement", "task", "--model", "haiku"], "invalid choice"),
    (["implement", "--resume", "s1"],
     "implement --resume requires explicit --model sonnet|opus"),
])
def test_invalid_model_rejected_before_provider(bridge, monkeypatch, capsys, argv, error):
    monkeypatch.setattr(sys, "argv", ["bridge", *argv])
    with pytest.raises(SystemExit) as exc:
        bridge.main()
    assert exc.value.code == 2
    assert error in capsys.readouterr().err
    assert not bridge.calls


@pytest.mark.parametrize("role", ["consult", "review"])
def test_readonly_resume_defaults_to_opus(bridge, monkeypatch, role):
    monkeypatch.setattr(sys, "argv", ["bridge", role, "task", "--resume", "s1"])
    assert bridge.main() == 0
    argv = bridge.calls[0].argv
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[-2:] == ["--resume", "s1"]


def test_implement_models_have_identical_permissions_prompt_and_environment(bridge):
    for model in ("sonnet", "opus"):
        bridge.events[0]["model"] = f"claude-{model}-fake"
        assert bridge.run("implement", "same task", model=model) == 0
    sonnet, opus = bridge.calls
    # Only the model argument differs, including settings/tools and all restrictions.
    assert sonnet.argv[:3] == opus.argv[:3]
    assert sonnet.argv[4:] == opus.argv[4:]
    assert sonnet.kwargs == opus.kwargs
    assert sonnet.stdin.saved == opus.stdin.saved


@pytest.mark.parametrize("requested,observed", [("sonnet", "opus"), ("opus", "sonnet")])
def test_explicit_opposite_observed_family_fails(bridge, capsys, requested, observed):
    bridge.events[0]["model"] = f"claude-{observed}-fake"
    assert bridge.run("implement", "task", model=requested) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["requested_model"] == requested
    assert report["observed_model"] == f"claude-{observed}-fake"
    assert report["success"] is False
    assert len(bridge.calls) == 1


@pytest.mark.parametrize("requested", ["sonnet", "opus"])
@pytest.mark.parametrize("observed", [None, "claude-x-fake"])
def test_missing_or_unknown_observed_model_is_left_to_lead_verification(
    bridge, capsys, requested, observed
):
    # Documents the current contract only: the adapter keeps no model registry, so a
    # missing/unrecognised observation is NOT verified here; the lead checks family match.
    bridge.events[0]["model"] = observed
    assert bridge.run("implement", "task", model=requested) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["requested_model"] == requested
    assert report["observed_model"] == observed
    assert report["success"] is True
    assert len(bridge.calls) == 1  # No adapter retry or fallback.


def test_git_snapshot_is_fixed_read_only(monkeypatch):
    module = load("claude-agent")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="snapshot")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert "snapshot" in module.git_context()
    assert [x[0] for x in calls] == [
        ["git", "status", "--short"], ["git", "diff", "--no-ext-diff", "--no-textconv"]
    ]
    assert all(kwargs["encoding"] == "utf-8" for _, kwargs in calls)


def test_watcher_render_and_sigint_does_not_signal_worker(tmp_path):
    watcher = load("claude-watch")
    assert "Öneri ve gerekçe" in watcher.render(events()[1])
    assert "TOOL Bash" in watcher.render(events()[1])
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "claude-watch.py"
    script.write_text((REPO / "scripts" / "claude-watch.py").read_text())
    log = tmp_path / "tmp" / "claude" / "sample.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps(events()[1]) + "\n")
    before = log.read_bytes()
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    observer = subprocess.Popen(
        [sys.executable, str(script), "tmp/claude/sample.jsonl"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert observer.stdout is not None
        assert "Öneri" in observer.stdout.readline()
        observer.send_signal(signal.SIGINT)
        observer.wait(timeout=5)
        assert observer.returncode == 0 and worker.poll() is None
        assert log.read_bytes() == before
    finally:
        if observer.poll() is None:
            observer.terminate()
        observer.wait(timeout=5)
        worker.terminate()
        worker.wait(timeout=5)
