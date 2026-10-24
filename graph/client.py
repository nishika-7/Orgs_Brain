"""FalkorDB connection plus a query helper that records a trace of every query.

The trace powers the "How I got this" panels: every analysis runs through run_query(), so we
always know which Cypher produced which answer. The trace is kept PER THREAD, because a web app
serves several visitors from one process and their traces must not mix.

If you already edited your own copy of this file (SSL, username...), keep your edits: the only
changes here are the per-thread trace and the optional FALKORDB_SSL setting.

Smoke test:  uv run python -m graph.client
"""
import threading
import time

from falkordb import FalkorDB

import config

_db = None
_graph = None


class _Trace:
    """A list-like object with a separate list for each thread."""

    def __init__(self):
        self._local = threading.local()

    def _items(self) -> list:
        if not hasattr(self._local, "items"):
            self._local.items = []
        return self._local.items

    def append(self, item) -> None:
        self._items().append(item)

    def clear(self) -> None:
        self._items().clear()

    def snapshot(self) -> list:
        return list(self._items())

    def __iter__(self):
        return iter(self._items())

    def __len__(self) -> int:
        return len(self._items())

    def __bool__(self) -> bool:
        return bool(self._items())

    def __getitem__(self, i):
        return self._items()[i]


# One entry per query: {"tool", "cypher", "params", "rows", "ms"}
trace = _Trace()


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
                ssl=getattr(config, "FALKOR_SSL", False),
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


def snapshot_trace() -> list:
    return trace.snapshot()


if __name__ == "__main__":
    result = get_graph().query("RETURN 1 AS ok")
    print("Connected to FalkorDB. RETURN 1 ->", result.result_set)
