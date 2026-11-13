"""Adapter protocol tests using a fake subprocess; never invoke Claude or change modes."""

import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
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


# Hardcoded baselines: normal roles must stay byte-identical when maintenance is added.
# Fake argv/settings coverage only; this is not proof of live permission enforcement.
BASE_COMMON = (
    "Türkçe cevap ver. AGENTS.md ve docs/WORKFLOW.md ile ilgili spec/kod/testleri oku. "
    "Yalnız verilen görevi yap; kapsam dışına çıkman gerekirse dur ve raporla. "
    "STATE yazma, git yazma, başka ajan/CLI çağırma, kalıcı ajan config değiştirme. "
    "İzin/kota/kimlik hatasında tekrar veya başka yol deneme; dur ve raporla. "
)
ENV_DENY = ["Read(./.env)", "Read(./.env.*)", "Read(./**/.env)", "Read(./**/.env.*)"]
BASE_TEST_PERMISSIONS = [
    "Bash(cd backend)", "Bash(cd frontend)",
    "Bash(.venv/bin/pytest *)", "Bash(.venv/bin/ruff check *)", "Bash(.venv/bin/mypy *)",
    "Bash(npm test *)", "Bash(npm run lint *)", "Bash(npm run typecheck *)",
    "Bash(npm run build *)", "Bash(npm run check:contrast *)",
    "Bash(npm test)", "Bash(npm run lint)", "Bash(npm run typecheck)",
    "Bash(npm run build)", "Bash(npm run check:contrast)",
]
IMPLEMENT_DENY = ENV_DENY + [
    "Edit(./STATE.md)", "Write(./STATE.md)", "Edit(./.claude/**)", "Write(./.claude/**)",
    "Edit(./.opencode/**)", "Write(./.opencode/**)", "Edit(./.git/**)", "Write(./.git/**)",
]
MAINT_ALLOW = [
    "Read", "Grep", "Glob",
    "Edit(./.opencode/commands/continue.md)", "Edit(./.opencode/commands/oturumabasla.md)",
]
MAINT_DENY = ENV_DENY + [
    "Edit(./STATE.md)", "Edit(./.claude/**)", "Edit(./.git/**)", "Edit(./.opencode/agents/**)",
]


def expected_argv(model, tools, allow, deny, disallowed):
    settings = {
        "disableAllHooks": True, "enabledPlugins": {}, "autoMemoryEnabled": False,
        "permissions": {"allow": allow, "deny": deny},
    }
    return [
        "claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose",
        "--setting-sources", "", "--settings", json.dumps(settings),
        "--permission-mode", "dontAsk", "--tools", tools,
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--disable-slash-commands", "--no-chrome", "--disallowedTools", disallowed,
    ]


NORMAL_BASELINE = {
    "implement": expected_argv(
        "sonnet", "Read,Grep,Glob,Edit,Write,Bash",
        ["Read", "Grep", "Glob", "Edit", "Write", *BASE_TEST_PERMISSIONS], IMPLEMENT_DENY,
        "Agent,Task,Skill,mcp__*",
    ),
    "consult": expected_argv("opus", "Read,Grep,Glob", ["Read", "Grep", "Glob"], ENV_DENY,
                             "Agent,Task,Skill,mcp__*,Bash,Edit,Write"),
    "review": expected_argv("opus", "Read,Grep,Glob", ["Read", "Grep", "Glob"], ENV_DENY,
                            "Agent,Task,Skill,mcp__*,Bash,Edit,Write"),
}


def test_common_text_unchanged(bridge):
    assert bridge.COMMON == BASE_COMMON


@pytest.mark.parametrize("role", ["implement", "consult", "review"])
def test_normal_roles_default_and_explicit_false_match_baseline(bridge, role):
    bridge.events[0]["model"] = "claude-sonnet-fake" if role == "implement" else None
    assert bridge.run(role, "same task") == 0
    assert bridge.run(role, "same task", maintain_commands=False) == 0
    default, explicit = bridge.calls
    assert default.argv == explicit.argv == NORMAL_BASELINE[role]
    assert default.kwargs == explicit.kwargs
    assert default.stdin.saved == explicit.stdin.saved
    assert bridge.command(role) == bridge.command(role, maintain_commands=False)
    assert bridge.command(role) == NORMAL_BASELINE[role]
    assert "--resume" not in default.argv
    assert default.stdin.saved.startswith(BASE_COMMON)
    assert bridge.MAINTENANCE_INSTRUCTION not in default.stdin.saved
    assert "Komut bakımı" not in default.stdin.saved
    if role == "implement":
        deny = json.loads(default.argv[default.argv.index("--settings") + 1])
        assert "Edit(./.opencode/**)" in deny["permissions"]["deny"]
        assert "Write(./.opencode/**)" in deny["permissions"]["deny"]
        assert "Yalnız görevde izin verilen dosyaları düzenle." in default.stdin.saved
    else:
        assert "Salt-okunur çalış." in default.stdin.saved


@pytest.mark.parametrize("role", ["implement", "consult", "review"])
def test_normal_resume_argv_unchanged(bridge, role):
    bridge.events[0]["model"] = "claude-opus-fake"
    assert bridge.run(role, "task", resume="s9", model="opus", maintain_commands=False) == 0
    base = NORMAL_BASELINE[role].copy()
    base[3] = "opus"
    assert bridge.calls[0].argv == [*base, "--resume", "s9"]


def test_maintenance_models_exact_permissions_and_prompt(bridge):
    for model in ("sonnet", "opus"):
        bridge.events[0]["model"] = f"claude-{model}-fake"
        assert bridge.run("implement", "Gövde görevi", model=model, maintain_commands=True) == 0
    sonnet, opus = bridge.calls
    for model, child in (("sonnet", sonnet), ("opus", opus)):
        assert child.argv == expected_argv(
            model, "Read,Grep,Glob,Edit", MAINT_ALLOW, MAINT_DENY,
            "Agent,Task,Skill,mcp__*,Bash,Write",
        )
        settings = json.loads(child.argv[child.argv.index("--settings") + 1])
        allow, deny = settings["permissions"]["allow"], settings["permissions"]["deny"]
        assert not any(x in {"Edit", "Write"} or x.startswith(("Bash", "Write")) for x in allow)
        assert "Edit(./.opencode/**)" not in deny  # Would kill the two narrow allows.
        assert "--resume" not in child.argv
        text = child.stdin.saved
        assert text.startswith(BASE_COMMON + bridge.MAINTENANCE_INSTRUCTION)
        assert "Gövde görevi" in text and "fixed read-only snapshot" in text
        assert "frontmatter unchanged" in text
        # Prompt policy only (no enforcement): no new shell templates/file refs/authority.
        assert "yeni !`...` shell template ekleme" in text
        assert "yeni @dosya referansı ekleme" in text
        assert "$ARGUMENTS kapsamını genişletme" in text
        assert "commit/push veya fetch yetkisini açık görev onayı" in text
        assert "mevcut fetch metni güncellenebilir" in text
        assert "test/lint" not in text and "Salt-okunur çalış." not in text
    assert sonnet.argv[4:] == opus.argv[4:]
    assert sonnet.kwargs == opus.kwargs
    assert sonnet.stdin.saved == opus.stdin.saved


@pytest.mark.parametrize("argv", [
    ["consult", "task", "--maintain-commands"],
    ["review", "task", "--maintain-commands"],
    ["consult", "task", "--maintain-commands", "--model", "opus"],
    ["implement", "task", "--maintain-commands", "--resume", "s1", "--model", "sonnet"],
    ["implement", "task", "--maintain-commands", "--resume", "s1"],
    ["implement", "task", "--maintain-commands", "--resume", ""],
    ["implement", "task", "--maint"],
    ["implement", "task", "--mod", "opus"],
    ["implement", "task", "--maintain-commands", "--log", "--resume", "s1"],
])
def test_maintenance_misuse_and_abbreviations_rejected_by_cli(
    bridge, monkeypatch, tmp_path, capsys, argv
):
    monkeypatch.setattr(bridge, "LOGS", tmp_path / "tmp" / "claude")
    monkeypatch.setattr(sys, "argv", ["bridge", *argv])
    with pytest.raises(SystemExit) as exc:
        bridge.main()
    assert exc.value.code == 2
    assert capsys.readouterr().err
    assert not bridge.calls
    assert not (tmp_path / "tmp").exists()


@pytest.mark.parametrize("role,resume", [
    ("consult", None), ("review", None), ("implement", "s1"), ("implement", ""),
    ("consult", "s1"),
])
def test_maintenance_direct_api_guard_before_git_and_provider(bridge, monkeypatch, role, resume):
    seen = []

    def git_context():
        seen.append("git")
        return "snapshot"

    monkeypatch.setattr(bridge, "git_context", git_context)
    with pytest.raises(ValueError, match="--maintain-commands"):
        bridge.run(role, "task", resume=resume, model="opus", maintain_commands=True)
    with pytest.raises(ValueError, match="--maintain-commands"):
        bridge.command(role, resume, "opus", maintain_commands=True)
    assert not seen and not bridge.calls


@pytest.mark.parametrize("use_log", [False, True])
@pytest.mark.parametrize("use_stdin", [False, True])
def test_maintenance_cli_happy_path(bridge, monkeypatch, tmp_path, capsys, use_log, use_stdin):
    bridge.events[0]["model"] = "claude-sonnet-fake"
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.setattr(bridge, "LOGS", tmp_path / "tmp" / "claude")
    task = "Yalnız iki komut gövdesi"
    argv = ["bridge", "implement", "--maintain-commands"]
    if use_stdin:
        monkeypatch.setattr(sys, "stdin", io.StringIO(task))
    else:
        argv.insert(2, task)
    if use_log:
        argv.append("--log")
    monkeypatch.setattr(sys, "argv", argv)
    assert bridge.main() == 0
    assert len(bridge.calls) == 1
    child = bridge.calls[0]
    assert child.argv == expected_argv(
        "sonnet", "Read,Grep,Glob,Edit", MAINT_ALLOW, MAINT_DENY,
        "Agent,Task,Skill,mcp__*,Bash,Write",
    )
    assert task in child.stdin.saved and task not in child.argv
    assert bridge.MAINTENANCE_INSTRUCTION in child.stdin.saved
    output = capsys.readouterr().out
    report = json.loads(output[output.index("{"):])
    assert report["success"] is True and report["requested_model"] == "sonnet"
    logs = list(bridge.LOGS.glob("*.jsonl")) if bridge.LOGS.exists() else []
    if use_log:
        assert len(logs) == 1 and f"tmp/claude/{logs[0].name}" in output
    else:
        assert not logs and "WATCH:" not in output


def test_maintenance_cli_opus_model(bridge, monkeypatch):
    bridge.events[0]["model"] = "claude-opus-fake"
    monkeypatch.setattr(sys, "argv", [
        "bridge", "implement", "task", "--maintain-commands", "--model", "opus",
    ])
    assert bridge.main() == 0
    assert bridge.calls[0].argv[:4] == ["claude", "-p", "--model", "opus"]
    assert bridge.calls[0].argv[bridge.calls[0].argv.index("--tools") + 1] == "Read,Grep,Glob,Edit"


@pytest.mark.parametrize("failure", ["denial", "mismatch", "exit"])
def test_maintenance_failure_is_not_success_and_not_retried(bridge, capsys, failure):
    if failure == "denial":
        bridge.events[-1]["permission_denials"] = [{"tool_name": "Edit"}]
    elif failure == "mismatch":
        bridge.events[0]["model"] = "claude-opus-fake"
    else:
        bridge.exit_code = 3
    assert bridge.run("implement", "task", maintain_commands=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["success"] is False
    if failure == "denial":
        assert report["permission_denial_details"] == [{"tool_name": "Edit"}]
    assert set(report) == {
        "role", "requested_model", "observed_model", "session_id", "exit_code", "success",
        "permission_denials", "permission_denial_details", "is_error", "result",
    }
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


def watcher_process(tmp_path, *args):
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    script = scripts / "claude-watch.py"
    script.write_text((REPO / "scripts" / "claude-watch.py").read_text())
    return subprocess.Popen(
        [sys.executable, str(script), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def read_until(observer, needle, timeout=10):
    """Read stdout lines until one contains needle; bounded by a reader thread."""
    seen = []

    def reader():
        assert observer.stdout is not None
        for line in observer.stdout:
            seen.append(line)
            if needle in line:
                return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    assert seen and needle in seen[-1], f"{needle!r} not seen in {seen!r}"
    return seen


def write_log(tmp_path, name, *items, complete=True):
    log = tmp_path / "tmp" / "claude" / name
    log.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(item) + "\n" for item in items)
    log.write_text(text if complete else text.rstrip("\n"))
    return log


def text_event(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def stop(observer):
    if observer.poll() is None:
        observer.send_signal(signal.SIGINT)
    observer.wait(timeout=5)
    for stream in (observer.stdout, observer.stderr):
        if stream is not None:
            stream.close()


def test_follow_latest_waits_without_creating_then_picks_new_log(tmp_path):
    observer = watcher_process(tmp_path, "--follow-latest")
    try:
        read_until(observer, "bekleniyor")
        assert not (tmp_path / "tmp").exists()
        write_log(tmp_path, "20260101-000001-000000-1.jsonl", text_event("ilk gorev"))
        read_until(observer, "ilk gorev")
        assert observer.poll() is None
    finally:
        stop(observer)
    assert observer.returncode == 0


def test_follow_latest_startup_picks_newest_and_ignores_old_append(tmp_path):
    old = write_log(tmp_path, "20260101-000001-000000-1.jsonl", text_event("eski"))
    write_log(tmp_path, "20260101-000002-000000-2.jsonl", text_event("yeni"))
    observer = watcher_process(tmp_path, "--follow-latest")
    try:
        lines = read_until(observer, "yeni")
        assert "eski" not in "".join(lines)
        assert "20260101-000002-000000-2.jsonl" in lines[0]
        with old.open("a") as handle:
            handle.write(json.dumps(text_event("eski ek")) + "\n")
        os.utime(old, (time.time() + 100, time.time() + 100))
        write_log(tmp_path, "20260101-000003-000000-3.jsonl", text_event("ucuncu"))
        lines = read_until(observer, "ucuncu")
        assert "eski ek" not in "".join(lines)
    finally:
        stop(observer)


def test_follow_latest_stays_open_after_result_and_switches_with_partial_buffer(tmp_path):
    write_log(tmp_path, "20260101-000001-000000-1.jsonl", *events()[1:])
    observer = watcher_process(tmp_path, "--follow-latest")
    try:
        lines = read_until(observer, "bekleniyor")
        assert "must-fix yok" in "".join(lines)
        write_log(tmp_path, "20260101-000002-000000-2.jsonl", text_event("ikinci"),
                  complete=False)
        partial = tmp_path / "tmp" / "claude" / "20260101-000002-000000-2.jsonl"
        partial.write_text(partial.read_text() + "\n" + json.dumps(text_event("yarim"))[:10])
        read_until(observer, "ikinci")
        # Switch away from an unfinished log with a partial line; the buffer must reset.
        write_log(tmp_path, "20260101-000003-000000-3.jsonl", text_event("ucuncu"))
        lines = read_until(observer, "ucuncu")
        assert '{"type": "assi' not in "".join(lines)
        assert observer.poll() is None
    finally:
        stop(observer)
    assert observer.returncode == 0


def test_follow_latest_sigint_does_not_signal_worker(tmp_path):
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    observer = watcher_process(tmp_path, "--follow-latest")
    try:
        read_until(observer, "bekleniyor")
        observer.send_signal(signal.SIGINT)
        observer.wait(timeout=5)
        assert observer.returncode == 0 and worker.poll() is None
        assert not (tmp_path / "tmp").exists()
    finally:
        stop(observer)
        worker.terminate()
        worker.wait(timeout=5)


def test_legacy_single_log_exits_on_result(tmp_path):
    write_log(tmp_path, "sample.jsonl", *events()[1:])
    observer = watcher_process(tmp_path, "tmp/claude/sample.jsonl")
    try:
        assert observer.wait(timeout=10) == 0
        assert observer.stdout is not None and "must-fix yok" in observer.stdout.read()
    finally:
        stop(observer)


@pytest.mark.parametrize("args", [[], ["--follow-latest", "tmp/claude/x.jsonl"]])
def test_watcher_requires_exactly_one_selection(tmp_path, args):
    observer = watcher_process(tmp_path, *args)
    try:
        assert observer.wait(timeout=10) == 2
        assert observer.stderr is not None and "--follow-latest" in observer.stderr.read()
    finally:
        stop(observer)
