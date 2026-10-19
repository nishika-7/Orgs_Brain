"""One command: parse a repo and load it into FalkorDB.

Usage (from the project root):
    uv run python -m ingest.run_ingest C:\\Users\\lenovo\\demo_repos\\flask --reset
    uv run python -m ingest.run_ingest                # uses DEMO_REPO_PATH from .env

--reset deletes the existing graph first (use it while developing so you start clean).
"""
import argparse
import sys
import time
from pathlib import Path

import config
from graph.client import get_graph, reset_graph, run_query
from ingest.load_graph import load_graph
from ingest.parse_code import parse_repo


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse a Python repo into a FalkorDB graph.")
    ap.add_argument("repo", nargs="?", default=None, help="path to the repo (default: DEMO_REPO_PATH)")
    ap.add_argument("--reset", action="store_true", help="delete the existing graph first")
    args = ap.parse_args()

    repo = Path(args.repo) if args.repo else config.DEMO_REPO_PATH
    if repo is None or not repo.is_dir():
        print("Give a repo path as an argument or set DEMO_REPO_PATH in .env")
        return 1
    repo = repo.resolve()

    print(f"Repo:  {repo}")
    print(f"Graph: {config.GRAPH_NAME} @ {config.FALKOR_HOST}:{config.FALKOR_PORT}")

    if args.reset:
        print("Resetting graph...")
        reset_graph()
    else:
        get_graph()

    t0 = time.time()
    print("Parsing...")
    parsed = parse_repo(repo)
    print(f"  parsed {len(parsed)} Python files in {time.time() - t0:.1f}s")

    t1 = time.time()
    print("Loading into FalkorDB...")
    stats = load_graph(parsed, root_name=repo.name)
    print(f"  loaded in {time.time() - t1:.1f}s")
    for key, value in stats.items():
        print(f"  {key:>14}: {value}")

    # ---- Sanity checks you can eyeball ----
    print("\nNodes by label:")
    res = run_query("sanity", "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS c ORDER BY c DESC")
    for label, count in res.result_set:
        print(f"  {label:>10}: {count}")

    print("\nRelationships by type:")
    res = run_query("sanity", "MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS c ORDER BY c DESC")
    for rel, count in res.result_set:
        print(f"  {rel:>10}: {count}")

    print("\nMost-called functions (top 5):")
    res = run_query(
        "sanity",
        """MATCH (c:Function)-[:CALLS]->(f:Function)
           RETURN f.qualname AS fn, f.file AS file, count(DISTINCT c) AS callers
           ORDER BY callers DESC LIMIT 5""",
    )
    for fn, file, callers in res.result_set:
        print(f"  {fn}  ({file})  <- {callers} callers")

    print("\nDone. Open FalkorDB Browser and try:  MATCH (n) RETURN n LIMIT 50")
    return 0


if __name__ == "__main__":
    sys.exit(main())
