"""Summarize an `opencode run --format json` event log for the /devam loop.

Usage: python3 -I .claude/skills/devam/summarize.py <events.jsonl>
Prints the session ID, errors, non-read tool calls, failed tool calls (WARN)
and the agent's final answer. Exits non-zero (status INCOMPLETE) unless the run
finished cleanly: a final `step_finish` with reason "stop", a session ID, a
final answer and no run-level error events.
"""

import json
import sys

READ_ONLY_TOOLS = {"read", "glob", "grep", "list", "todowrite"}


def main(path: str) -> int:
    session = None
    final_text = None
    last_text = None
    last_finish_reason = None
    problems: list[str] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                if line.strip():
                    print("OUTPUT:", line.strip()[:300])
                continue
            session = event.get("sessionID") or session
            kind = event.get("type")
            part = event.get("part", {})
            if kind == "error":
                data = event.get("error", {}).get("data", {})
                problems.append(f"error: {data.get('message') or event.get('error')}")
            elif kind == "tool_use":
                state = part.get("state", {})
                status = state.get("status")
                if status not in ("completed", None):
                    # The agent may recover from a failed tool call; surface it, but judge
                    # completion by the final step instead.
                    print(f"WARN tool {part.get('tool')} {status}:", str(state.get("error"))[:200])
                if part.get("tool") not in READ_ONLY_TOOLS:
                    print(f"TOOL {part.get('tool')} [{status}]:", str(state.get("input"))[:200])
            elif kind == "text" and part.get("text"):
                last_text = part["text"]
                phase = part.get("metadata", {}).get("openai", {}).get("phase")
                if phase in (None, "final_answer"):
                    final_text = part["text"]
            elif kind == "step_finish":
                last_finish_reason = part.get("reason")

    if last_finish_reason != "stop":
        problems.append(f"run did not finish cleanly (last step_finish reason: {last_finish_reason})")
    if session is None:
        problems.append("no session ID")
    if final_text is None:
        problems.append("no final answer")

    print("SESSION:", session)
    for problem in problems:
        print("PROBLEM:", problem)
    print("STATUS:", "INCOMPLETE" if problems else "OK")
    print("--- final answer ---")
    print(final_text or last_text or "(no text)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
