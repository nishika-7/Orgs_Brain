"""Terminal chat with the agent.

    uv run python chat.py

Commands:
    /diff path/to/change.diff   load a diff (create one with:  git diff > change.diff  inside the repo)
    /trace                      show the Cypher queries behind the last answer
    /quit
"""
from pathlib import Path

from agent.agent import build_agent
from agent.orchestrate import ask
from agent.tools import set_current_diff
from graph import client


def show_tools_used() -> None:
    if not client.trace:
        return
    tools = {}
    for t in client.trace:
        tools.setdefault(t["tool"], []).append(t["rows"])
    print("  [graph] " + "; ".join(f"{k} x{len(v)}" for k, v in tools.items()))


def main() -> None:
    agent = build_agent()
    print("Safe-Change Guide. Ask what is safe to change and who to ask. /diff <file>, /trace, /quit")
    while True:
        try:
            q = input("\nYou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if q == "/quit":
            break
        if q == "/trace":
            for t in client.trace:
                print(f"  {t['tool']} ({t['rows']} rows, {t['ms']} ms): {t['cypher']}  {t['params']}")
            continue
        if q.startswith("/diff"):
            path = Path(q[5:].strip().strip('"'))
            if not path.is_file():
                print("  File not found.")
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            set_current_diff(text)
            n_files = text.count("\ndiff --git") + (1 if text.startswith("diff --git") else 0)
            q = f"A diff is loaded ({n_files} file(s)). Analyze this change: what could break and who should review it?"
            print(f"  Loaded {path.name}")
        client.clear_trace()
        answer, prep = ask(agent, q)
        print(f"\nGuide> {answer}")
        if prep.used:
            print("  [evidence] " + ", ".join(prep.used))
        show_tools_used()


if __name__ == "__main__":
    main()
