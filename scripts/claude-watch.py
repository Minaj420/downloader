"""Read-only live log observer; never owns or signals the worker process."""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def render(event):
    kind = event.get("type")
    if kind in {"assistant", "user"}:
        lines = []
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                lines.append(f"TOOL {block.get('name')}: {json.dumps(block.get('input'))}")
            elif block.get("type") == "tool_result":
                lines.append(f"TOOL RESULT error={block.get('is_error', False)}: "
                             f"{block.get('content')}")
            elif block.get("type") == "text":
                lines.append(block.get("text", ""))
        return "\n".join(lines)
    return json.dumps(event, ensure_ascii=False)


def show(pending):
    """Print one complete line; return an exit code once it is the final result."""
    try:
        event = json.loads(pending)
        print(render(event), flush=True)
        if event.get("type") == "result":
            return 1 if event.get("is_error") or event.get("permission_denials") else 0
    except ValueError:
        print(pending.rstrip(), flush=True)
    return None


def newest_log():
    """Newest adapter log by filename (timestamp-pid); never creates anything."""
    try:
        logs = (ROOT / "tmp" / "claude").resolve()
        if not logs.is_relative_to(ROOT.resolve()):
            return None
        names = []
        for entry in logs.glob("*.jsonl"):
            real = entry.resolve()
            if real.parent == logs and real.is_file():
                names.append(entry)
        return max(names, key=lambda entry: entry.name, default=None)
    except OSError:
        return None


def follow_latest():
    """Follow the newest log, switching to newer ones; never exits on a result."""
    waiting = "Yeni görev bekleniyor..."
    current = None
    handle = None
    pending = ""
    announced = False
    next_scan = 0.0
    try:
        while True:
            if time.monotonic() >= next_scan:
                next_scan = time.monotonic() + 1.0
                latest = newest_log()
                if latest is not None and (current is None or latest.name > current):
                    try:
                        opened = latest.open(encoding="utf-8")
                    except OSError:
                        opened = None
                    if opened is not None:
                        if handle is not None:
                            handle.close()
                        handle, current, pending, announced = opened, latest.name, "", False
                        print(f"=== Log: {current} ===", flush=True)
            if handle is not None:
                line = handle.readline()
                if line:
                    pending += line
                    if pending.endswith("\n"):
                        done = show(pending) is not None
                        pending = ""
                        if done:
                            handle.close()
                            handle = None
                    continue
            if handle is None and not announced:
                print(waiting, flush=True)
                announced = True
            time.sleep(0.1 if handle is not None else 1.0)
    finally:
        if handle is not None:
            handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", nargs="?",
                        help="JSONL log under tmp/claude (relative to repo root)")
    parser.add_argument("--follow-latest", action="store_true",
                        help="follow the newest tmp/claude log and switch to new ones "
                             "(does not exit on a result; use Ctrl+C)")
    args = parser.parse_args()
    if args.follow_latest == (args.log is not None):
        parser.error("Provide either a log path or --follow-latest, not both or neither")
    try:
        if args.follow_latest:
            follow_latest()
            return 0
        path = (ROOT / args.log).resolve()
        if not path.is_relative_to((ROOT / "tmp" / "claude").resolve()):
            parser.error("Log must be under tmp/claude")
        with path.open(encoding="utf-8") as log:
            pending = ""
            while True:
                line = log.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                pending += line
                if not pending.endswith("\n"):
                    continue
                code = show(pending)
                if code is not None:
                    return code
                pending = ""
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    except OSError as exc:
        print(f"Observer NOT RUN: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
