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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", help="JSONL log under tmp/claude (relative to repo root)")
    args = parser.parse_args()
    path = (ROOT / args.log).resolve()
    if not path.is_relative_to((ROOT / "tmp" / "claude").resolve()):
        parser.error("Log must be under tmp/claude")
    try:
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
                try:
                    event = json.loads(pending)
                    print(render(event), flush=True)
                    if event.get("type") == "result":
                        return 1 if event.get("is_error") or event.get("permission_denials") else 0
                except ValueError:
                    print(pending.rstrip(), flush=True)
                pending = ""
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    except OSError as exc:
        print(f"Observer NOT RUN: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
