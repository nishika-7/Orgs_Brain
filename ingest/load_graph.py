"""Write the parsed repo into FalkorDB.

Everything uses MERGE, so running the loader twice does not create duplicates.
Writes are batched with UNWIND (one round trip per ~200 rows instead of one per row).

Graph schema created here:
  (Directory)-[:CONTAINS]->(Directory|File)
  (File)-[:DEFINES]->(Function|Class)
  (File)-[:IMPORTS {names, lines}]->(File)
  (File)-[:USES]->(Package)
  (Function)-[:CALLS {sites}]->(Function)             sites = "12,40" (lines of the calls)
  (Function)-[:REFERENCES {sites}]->(Function)        function passed as a value (callbacks)
  (Function)-[:INSTANTIATES {sites}]->(Class)
  (Class)-[:INHERITS]->(Class)
"""
from pathlib import PurePosixPath

import config
from graph.client import get_graph
from ingest import resolve


def _batches(rows: list, size: int):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _write(cypher: str, rows: list) -> None:
    g = get_graph()
    for chunk in _batches(rows, config.BATCH_SIZE):
        g.query(cypher, {"rows": chunk})


def _sites(lines) -> str:
    return ",".join(str(n) for n in sorted(set(lines))[:20])


def _ensure_indexes() -> None:
    g = get_graph()
    for stmt in (
        "CREATE INDEX FOR (f:File) ON (f.path)",
        "CREATE INDEX FOR (d:Directory) ON (d.path)",
        "CREATE INDEX FOR (fn:Function) ON (fn.uid)",
        "CREATE INDEX FOR (fn:Function) ON (fn.name)",
        "CREATE INDEX FOR (fn:Function) ON (fn.file)",
        "CREATE INDEX FOR (c:Class) ON (c.uid)",
        "CREATE INDEX FOR (p:Package) ON (p.name)",
    ):
        try:
            g.query(stmt)
        except Exception:
            pass  # already exists


def load_graph(parsed: list[dict], root_name: str | None = None) -> dict:
    """Load everything. Returns a small stats dict for printing."""
    _ensure_indexes()
    resolver = resolve.ImportResolver([pf["path"] for pf in parsed], root_name)

    # ---- Pass 1: structure (directories, files, functions, classes) ----
    dirs, dir_edges, file_edges = resolve.directory_structure([pf["path"] for pf in parsed])
    _write(
        "UNWIND $rows AS row MERGE (d:Directory {path: row.path}) SET d.name = row.name",
        [{"path": d, "name": PurePosixPath(d).name or "."} for d in sorted(dirs)],
    )
    _write(
        """UNWIND $rows AS row
           MATCH (a:Directory {path: row.parent}), (b:Directory {path: row.child})
           MERGE (a)-[:CONTAINS]->(b)""",
        [{"parent": a, "child": b} for a, b in sorted(dir_edges)],
    )
    _write(
        """UNWIND $rows AS row
           MERGE (f:File {path: row.path})
           SET f.name = row.name, f.loc = row.loc, f.is_test = row.is_test, f.doc = row.doc,
               f.language = 'python'""",
        [
            {
                "path": pf["path"],
                "name": PurePosixPath(pf["path"]).name,
                "loc": pf["loc"],
                "is_test": pf["is_test"],
                "doc": pf["doc"],
            }
            for pf in parsed
        ],
    )
    _write(
        """UNWIND $rows AS row
           MATCH (d:Directory {path: row.dir}), (f:File {path: row.file})
           MERGE (d)-[:CONTAINS]->(f)""",
        [{"dir": d, "file": f} for d, f in sorted(file_edges)],
    )

    fn_rows, cls_rows = [], []
    for pf in parsed:
        for fn in pf["functions"]:
            fn_rows.append(
                {
                    "uid": resolve.function_uid(pf["path"], fn["qualname"]),
                    "file": pf["path"],
                    "name": fn["name"],
                    "qualname": fn["qualname"],
                    "lineno": fn["lineno"],
                    "end_lineno": fn["end_lineno"],
                    "indent": fn["indent"],
                    "doc": fn["doc"],
                    "is_test": pf["is_test"],
                    "decorators": fn["decorators"],
                    "is_public": not fn["name"].startswith("_"),
                    "nested_in": fn["nested_in"],
                    "escapes": fn["escapes"],
                    "returns_callable": fn["returns_callable"],
                    "dynamic": fn["dynamic"],
                }
            )
        for cls in pf["classes"]:
            cls_rows.append(
                {
                    "uid": resolve.function_uid(pf["path"], cls["qualname"]),
                    "file": pf["path"],
                    "name": cls["name"],
                    "qualname": cls["qualname"],
                    "lineno": cls["lineno"],
                    "end_lineno": cls["end_lineno"],
                    "indent": cls["indent"],
                    "doc": cls["doc"],
                }
            )
    _write(
        """UNWIND $rows AS row
           MATCH (f:File {path: row.file})
           MERGE (fn:Function {uid: row.uid})
           SET fn.name = row.name, fn.qualname = row.qualname, fn.file = row.file,
               fn.lineno = row.lineno, fn.end_lineno = row.end_lineno, fn.indent = row.indent,
               fn.doc = row.doc, fn.is_test = row.is_test, fn.decorators = row.decorators,
               fn.is_public = row.is_public, fn.nested_in = row.nested_in,
               fn.escapes = row.escapes, fn.returns_callable = row.returns_callable,
               fn.dynamic = row.dynamic
           MERGE (f)-[:DEFINES]->(fn)""",
        fn_rows,
    )
    _write(
        """UNWIND $rows AS row
           MATCH (f:File {path: row.file})
           MERGE (c:Class {uid: row.uid})
           SET c.name = row.name, c.qualname = row.qualname, c.file = row.file,
               c.lineno = row.lineno, c.end_lineno = row.end_lineno, c.indent = row.indent,
               c.doc = row.doc
           MERGE (f)-[:DEFINES]->(c)""",
        cls_rows,
    )

    # ---- Pass 2: relationships between things (needs all nodes to exist) ----
    file_imports, package_uses, imported = resolve.resolve_imports(parsed, resolver)
    _write(
        """UNWIND $rows AS row
           MATCH (a:File {path: row.src}), (b:File {path: row.dst})
           MERGE (a)-[e:IMPORTS]->(b)
           SET e.names = row.names, e.lines = row.lines""",
        [
            {
                "src": src,
                "dst": dst,
                "names": ",".join(sorted(info["names"]))[:300],
                "lines": _sites(info["lines"]),
            }
            for (src, dst), info in sorted(file_imports.items())
        ],
    )
    _write(
        """UNWIND $rows AS row
           MATCH (a:File {path: row.src})
           MERGE (p:Package {name: row.pkg})
           MERGE (a)-[:USES]->(p)""",
        [{"src": a, "pkg": p} for a, p in sorted(package_uses)],
    )

    calls, refs = resolve.resolve_calls(parsed, imported)
    for rel_type, edges in (("CALLS", calls), ("REFERENCES", refs)):
        _write(
            f"""UNWIND $rows AS row
                MATCH (a:Function {{uid: row.a}}), (b:Function {{uid: row.b}})
                MERGE (a)-[e:{rel_type}]->(b)
                SET e.sites = row.sites""",
            [{"a": a, "b": b, "sites": _sites(lines)} for (a, b), lines in sorted(edges.items())],
        )

    inherits = resolve.resolve_inheritance(parsed, imported)
    _write(
        """UNWIND $rows AS row
           MATCH (a:Class {uid: row.a}), (b:Class {uid: row.b})
           MERGE (a)-[:INHERITS]->(b)""",
        [{"a": a, "b": b} for a, b in sorted(inherits)],
    )

    instantiates = resolve.resolve_instantiations(parsed, imported)
    _write(
        """UNWIND $rows AS row
           MATCH (a:Function {uid: row.a}), (b:Class {uid: row.b})
           MERGE (a)-[e:INSTANTIATES]->(b)
           SET e.sites = row.sites""",
        [{"a": a, "b": b, "sites": _sites(lines)} for (a, b), lines in sorted(instantiates.items())],
    )

    return {
        "files": len(parsed),
        "functions": len(fn_rows),
        "classes": len(cls_rows),
        "import_edges": len(file_imports),
        "package_edges": len(package_uses),
        "call_edges": len(calls),
        "reference_edges": len(refs),
        "inherit_edges": len(inherits),
        "instantiate_edges": len(instantiates),
    }
