"""Thin subscription CLI adapter; orchestration and review policy belong to the lead."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "tmp" / "claude"
TEST_PERMISSIONS = [
    "Bash(cd backend)", "Bash(cd frontend)",
    "Bash(.venv/bin/pytest *)", "Bash(.venv/bin/ruff check *)", "Bash(.venv/bin/mypy *)",
    "Bash(npm test *)", "Bash(npm run lint *)", "Bash(npm run typecheck *)",
    "Bash(npm run build *)", "Bash(npm run check:contrast *)",
    "Bash(npm test)", "Bash(npm run lint)", "Bash(npm run typecheck)",
    "Bash(npm run build)", "Bash(npm run check:contrast)",
]
COMMON = (
    "Türkçe cevap ver. AGENTS.md ve docs/WORKFLOW.md ile ilgili spec/kod/testleri oku. "
    "Yalnız verilen görevi yap; kapsam dışına çıkman gerekirse dur ve raporla. "
    "STATE yazma, git yazma, başka ajan/CLI çağırma, kalıcı ajan config değiştirme. "
    "İzin/kota/kimlik hatasında tekrar veya başka yol deneme; dur ve raporla. "
)


def command(role, resume=None):
    tools = ["Read", "Grep", "Glob"]
    allow = tools.copy()
    deny = ["Read(./.env)", "Read(./.env.*)", "Read(./**/.env)", "Read(./**/.env.*)"]
    if role == "implement":
        tools += ["Edit", "Write", "Bash"]
        allow += ["Edit", "Write", *TEST_PERMISSIONS]
        deny += ["Edit(./STATE.md)", "Write(./STATE.md)",
                 "Edit(./.claude/**)", "Write(./.claude/**)",
                 "Edit(./.opencode/**)", "Write(./.opencode/**)",
                 "Edit(./.git/**)", "Write(./.git/**)"]
    settings = {
        "disableAllHooks": True, "enabledPlugins": {}, "autoMemoryEnabled": False,
        "permissions": {"allow": allow, "deny": deny},
    }
    argv = [
        "claude", "-p", "--model", "opus", "--output-format", "stream-json", "--verbose",
        "--setting-sources", "", "--settings", json.dumps(settings),
        "--permission-mode", "dontAsk", "--tools", ",".join(tools),
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--disable-slash-commands", "--no-chrome", "--disallowedTools",
        "Agent,Task,Skill,mcp__*" + (",Bash,Edit,Write" if role != "implement" else ""),
    ]
    if resume:
        argv += ["--resume", resume]
    return argv


def environment():
    # Keep subscription HOME/keychain, not API/provider overrides or nested-host settings.
    env = {k: v for k, v in os.environ.items() if not
           k.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE", "MCP_"))}
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
        if name in os.environ:
            env[name] = os.environ[name]
    env["CLAUDE_CODE_SAFE_MODE"] = "1"
    return env


def git_context():
    sections = []
    for args in (["status", "--short"], ["diff", "--no-ext-diff", "--no-textconv"]):
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True
        )
        sections.append(f"git {args}:\n{result.stdout}")
    return "\n".join(sections)


def run(role, prompt, resume=None, log=None):
    instruction = (
        "Yalnız görevde izin verilen dosyaları düzenle. Önceden kararlaştırılmış hedefli "
        "test/lint komutlarını kendin çalıştır; sonuçları PASS/FAIL/NOT RUN olarak raporla. "
        "Bash yalnız backend/frontend hedefli kontroller içindir. İlgili dizine geçmek "
        "için ayrı cd backend veya cd frontend çağrısı yap; zincir/pipe/yönlendirme kullanma. "
        "Dosya okuma/arama için Read/Grep/Glob kullan; Bash ile git/ls/cat denemesi yapma. "
        if role == "implement" else
        "Salt-okunur çalış. Öneri/gerekçe/alternatif-risk yaz. Review ise bulguları "
        "must-fix/should-fix/accepted-risk/over-engineering sınıfla; dosya:satır ve somut "
        "düzeltme ver. Son satır: must-fix yok veya must-fix var (N). "
    )
    text = COMMON + instruction + "\nGörev:\n" + prompt + "\nGit snapshot:\n" + git_context()
    final = None
    model = session = None
    denied = malformed = False
    denial_details = []
    with subprocess.Popen(
        command(role, resume), cwd=ROOT, env=environment(), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True, encoding="utf-8",  # stderr stays in owning terminal.
    ) as child:
        child.stdin.write(text)
        child.stdin.close()
        for line in child.stdout:
            if log:
                log.write(line)
                log.flush()
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Not an event")
            except ValueError:
                malformed = True
                continue
            if event.get("subtype") == "init":
                model, session = event.get("model"), event.get("session_id")
            denied |= bool(event.get("permission_denials"))
            denied |= event.get("subtype") == "permission_denied"
            details = event.get("permission_denials")
            if details:
                denial_details.extend(details if isinstance(details, list) else [details])
            if event.get("subtype") == "permission_denied":
                denial_details.append(event)
            if event.get("type") == "result":
                final = event
    success = (
        child.returncode == 0 and final is not None and not malformed and not denied
        and final.get("is_error") is False and final.get("subtype") == "success"
        and isinstance(final.get("result"), str) and bool(final["result"].strip())
    )
    report = {
        "role": role, "requested_model": "opus", "observed_model": model,
        "session_id": (final or {}).get("session_id", session), "exit_code": child.returncode,
        "success": success, "permission_denials": denied,
        "permission_denial_details": denial_details,
        "is_error": (final or {}).get("is_error"), "result": (final or {}).get("result"),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("consult", "review", "implement"))
    parser.add_argument("prompt", nargs="?", help="Omit to read plain text from stdin")
    parser.add_argument("--resume", metavar="SESSION_ID")
    parser.add_argument("--log", action="store_true", help="Save stream JSONL under tmp/claude")
    args = parser.parse_args()
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not prompt.strip():
        parser.error("Nonempty prompt required")
    try:
        if not args.log:
            return run(args.role, prompt, args.resume)
        if LOGS.is_symlink() or (ROOT / "tmp").is_symlink():
            raise ValueError("Symlink log directories are not supported")
        LOGS.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now():%Y%m%d-%H%M%S-%f}-{os.getpid()}.jsonl"
        path = LOGS / name
        with path.open("x", encoding="utf-8") as log:
            print(f"WATCH: bash scripts/claude-watch.sh tmp/claude/{name}", flush=True)
            return run(args.role, prompt, args.resume, log)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"FAIL/INCOMPLETE: {exc}; no retry/fallback", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
