"""FalkorDB connection plus a query helper that records a trace of every query.

The trace is what powers the "How I got this" box in the UI later: every tool
calls run_query(), so we always know which Cypher produced which answer.

Smoke test:  uv run python -m graph.client
"""
import time

from falkordb import FalkorDB

import config 
import os
from dotenv import load_dotenv
load_dotenv()

_db = None
_graph = None

# One entry per query: {"tool", "cypher", "params", "rows", "ms"}
trace: list[dict] = []


def get_graph():
    """Return the (cached) graph handle, connecting on first use."""
    global _db, _graph
    if _graph is None:
        if _db is None:
            _db = FalkorDB(
                host=config.FALKOR_HOST,
                port=config.FALKOR_PORT,
                username=config.FALKOR_USERNAME,
                password=config.FALKOR_PASSWORD,
                ssl=config.FALKOR_SSL
            )
        _graph = _db.select_graph(config.GRAPH_NAME)
    return _graph


def reset_graph():
    """Delete the whole graph and return a fresh, empty handle."""
    global _graph
    g = get_graph()
    try:
        g.delete()
    except Exception as exc:  # the graph may not exist yet on the first run
        print(f"(reset) nothing to delete: {exc}")
    _graph = None
    return get_graph()


def run_query(tool: str, cypher: str, params: dict | None = None, read_only: bool = True):
    """Run a query and log it. Agent tools should always use read_only=True."""
    g = get_graph()
    start = time.time()
    runner = g.ro_query if read_only else g.query
    res = runner(cypher, params or {})
    trace.append(
        {
            "tool": tool,
            "cypher": " ".join(cypher.split()),
            "params": params or {},
            "rows": len(res.result_set),
            "ms": round((time.time() - start) * 1000),
        }
    )
    return res


def clear_trace() -> None:
    trace.clear()


if __name__ == "__main__":
    result = get_graph().query("RETURN 1 AS ok")
    print("Connected to FalkorDB. RETURN 1 ->", result.result_set)
