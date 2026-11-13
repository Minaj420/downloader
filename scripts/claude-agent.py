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
# Opt-in command maintenance: fixed paths only; callers cannot pass paths or wildcards.
MAINTENANCE_TOOLS = ["Read", "Grep", "Glob", "Edit"]
MAINTENANCE_ALLOW = [
    "Read", "Grep", "Glob",
    "Edit(./.opencode/commands/continue.md)", "Edit(./.opencode/commands/oturumabasla.md)",
]
MAINTENANCE_DENY = [
    "Read(./.env)", "Read(./.env.*)", "Read(./**/.env)", "Read(./**/.env.*)",
    "Edit(./STATE.md)", "Edit(./.claude/**)", "Edit(./.git/**)", "Edit(./.opencode/agents/**)",
]
MAINTENANCE_DISALLOWED = "Agent,Task,Skill,mcp__*,Bash,Write"
MAINTENANCE_INSTRUCTION = (
    "Komut bakımı modu: yalnız MEVCUT .opencode/commands/continue.md ve "
    ".opencode/commands/oturumabasla.md dosyalarının gövdesini Edit ile düzenle. Her dosyanın "
    "başındaki ilk --- ... --- frontmatter bloğu bayt bayt değişmez. Bash/Write, test "
    "çalıştırma, başka config veya dosya düzenleme ve yeni dosya oluşturma yok. Yalnız bu iki "
    "gövde, normal kalıcı ajan config değiştirme yasağının kullanıcı onaylı dar istisnasıdır; "
    "diğer her yerde yasak geçerlidir. Gövde lead talimatıdır: yeni !`...` shell template "
    "ekleme, yeni @dosya referansı ekleme, $ARGUMENTS kapsamını genişletme; izin/ajan/model/"
    "araç/commit/push veya fetch yetkisini açık görev onayı ve mevcut docs/WORKFLOW.md "
    "sınırları dışında genişleten talimat yazma. Onaylı WORKFLOW akışıyla uyum için mevcut "
    "fetch metni güncellenebilir. Raporda düzenlenen dosyaları ve frontmatter'ın "
    "değişmediğini (frontmatter unchanged) açıkça yaz. "
)


def check_maintenance(role, resume, maintain_commands):
    if maintain_commands and (role != "implement" or resume is not None):
        raise ValueError("--maintain-commands requires implement without --resume")


def selected_model(role, model=None):
    model = model or ("sonnet" if role == "implement" else "opus")
    if model not in ("sonnet", "opus"):
        raise ValueError("--model must be sonnet or opus")
    if role != "implement" and model != "opus":
        raise ValueError(f"{role} requires --model opus; sonnet is only allowed for implement")
    return model


def command(role, resume=None, model=None, *, maintain_commands=False):
    check_maintenance(role, resume, maintain_commands)
    model = selected_model(role, model)
    if maintain_commands:
        return maintenance_command(model)
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
        "claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose",
        "--setting-sources", "", "--settings", json.dumps(settings),
        "--permission-mode", "dontAsk", "--tools", ",".join(tools),
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--disable-slash-commands", "--no-chrome", "--disallowedTools",
        "Agent,Task,Skill,mcp__*" + (",Bash,Edit,Write" if role != "implement" else ""),
    ]
    if resume:
        argv += ["--resume", resume]
    return argv


def maintenance_command(model):
    # Separate set: no broad .opencode deny here, otherwise the two allows would be dead.
    settings = {
        "disableAllHooks": True, "enabledPlugins": {}, "autoMemoryEnabled": False,
        "permissions": {"allow": MAINTENANCE_ALLOW.copy(), "deny": MAINTENANCE_DENY.copy()},
    }
    return [
        "claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose",
        "--setting-sources", "", "--settings", json.dumps(settings),
        "--permission-mode", "dontAsk", "--tools", ",".join(MAINTENANCE_TOOLS),
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--disable-slash-commands", "--no-chrome", "--disallowedTools", MAINTENANCE_DISALLOWED,
    ]


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


def run(role, prompt, resume=None, log=None, model=None, *, maintain_commands=False):
    check_maintenance(role, resume, maintain_commands)  # Before git_context/Popen.
    requested_model = selected_model(role, model)
    instruction = MAINTENANCE_INSTRUCTION if maintain_commands else (
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
    argv = command(role, resume, requested_model, maintain_commands=maintain_commands)
    with subprocess.Popen(
        argv, cwd=ROOT, env=environment(), stdin=subprocess.PIPE,
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
    # Reject an explicit opposite family; unknown/missing IDs remain for lead verification.
    opposite = "opus" if requested_model == "sonnet" else "sonnet"
    model_mismatch = isinstance(model, str) and opposite in model.lower().split("-")
    success = (
        child.returncode == 0 and final is not None and not malformed and not denied
        and not model_mismatch
        and final.get("is_error") is False and final.get("subtype") == "success"
        and isinstance(final.get("result"), str) and bool(final["result"].strip())
    )
    report = {
        "role": role, "requested_model": requested_model, "observed_model": model,
        "session_id": (final or {}).get("session_id", session), "exit_code": child.returncode,
        "success": success, "permission_denials": denied,
        "permission_denial_details": denial_details,
        "is_error": (final or {}).get("is_error"), "result": (final or {}).get("result"),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("role", choices=("consult", "review", "implement"))
    parser.add_argument("prompt", nargs="?", help="Omit to read plain text from stdin")
    parser.add_argument("--resume", metavar="SESSION_ID")
    parser.add_argument("--model", choices=("sonnet", "opus"),
                        help="implement defaults to sonnet; consult/review require opus")
    parser.add_argument("--log", action="store_true", help="Save stream JSONL under tmp/claude")
    parser.add_argument("--maintain-commands", action="store_true",
                        help="implement only, no --resume: edit bodies of continue.md and "
                             "oturumabasla.md under .opencode/commands; no Bash/Write")
    args = parser.parse_args()
    try:
        check_maintenance(args.role, args.resume, args.maintain_commands)
    except ValueError as exc:
        parser.error(str(exc))
    if args.resume and args.role == "implement" and args.model is None:
        parser.error("implement --resume requires explicit --model sonnet|opus")
    try:
        model = selected_model(args.role, args.model)
    except ValueError as exc:
        parser.error(str(exc))
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not prompt.strip():
        parser.error("Nonempty prompt required")
    try:
        if not args.log:
            return run(args.role, prompt, args.resume, model=model,
                       maintain_commands=args.maintain_commands)
        if LOGS.is_symlink() or (ROOT / "tmp").is_symlink():
            raise ValueError("Symlink log directories are not supported")
        LOGS.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now():%Y%m%d-%H%M%S-%f}-{os.getpid()}.jsonl"
        path = LOGS / name
        with path.open("x", encoding="utf-8") as log:
            print(f"WATCH: bash scripts/claude-watch.sh tmp/claude/{name}", flush=True)
            return run(args.role, prompt, args.resume, log, model=model,
                       maintain_commands=args.maintain_commands)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"FAIL/INCOMPLETE: {exc}; no retry/fallback", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
