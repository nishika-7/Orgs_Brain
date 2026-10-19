"""Graph analysis for the "what is safe to change, and who do I ask?" agent.

Plain Python + Cypher. No LLM and no agent framework in this file, so it can be
tested on its own. The Strands tools in tools.py are thin wrappers around it.

Everything here is STATIC analysis: it finds the dependents it can see in the
code. It can never prove that a change is safe (dynamic dispatch, reflection,
string-based wiring and outside consumers are invisible), so results always say
"known dependents", never "safe".
"""
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
import os
from dotenv import load_dotenv
load_dotenv()

import config
from graph.client import run_query
import os
MAX_DEPTH = 4

COCHANGE_MAX_FILES = 25  # commits touching more files than this are bulk edits, not evidence of coupling


# ------------------------------------------------------------------ finding things

def _fn(row) -> dict:
    return {"uid": row[0], "qualname": row[1], "file": row[2], "lineno": row[3], "end_lineno": row[4]}


def lookup_functions(target: str, limit: int = 10) -> list[dict]:
    """Exact lookup by uid ('app/web.py::signup_view'), qualified name, or simple name."""
    t = target.strip()
    if "::" in t:
        cypher = "MATCH (fn:Function {uid: $t}) RETURN fn.uid, fn.qualname, fn.file, fn.lineno, fn.end_lineno"
    else:
        cypher = f"""MATCH (fn:Function) WHERE fn.qualname = $t OR fn.name = $t
                     RETURN fn.uid, fn.qualname, fn.file, fn.lineno, fn.end_lineno
                     ORDER BY fn.is_test, size(fn.file) LIMIT {int(limit)}"""
    return [_fn(r) for r in run_query("lookup", cypher, {"t": t}).result_set]


def search_functions(text: str, limit: int = 10) -> list[dict]:
    """Fuzzy search: substring match on the qualified name, exact name matches first."""
    cypher = f"""MATCH (fn:Function)
                 WHERE toLower(fn.qualname) CONTAINS toLower($t)
                 RETURN fn.uid, fn.qualname, fn.file, fn.lineno, fn.end_lineno
                 ORDER BY CASE WHEN fn.name = $t THEN 0 ELSE 1 END, fn.is_test, size(fn.qualname)
                 LIMIT {int(limit)}"""
    return [_fn(r) for r in run_query("find_functions", cypher, {"t": text.strip()}).result_set]


def function_details(uid: str) -> dict | None:
    rows = run_query(
        "function_details",
        """MATCH (fn:Function {uid: $u})
           RETURN fn.uid, fn.qualname, fn.file, fn.lineno, fn.end_lineno,
                  fn.decorators, fn.is_public, fn.is_test, fn.last_modified, fn.doc,
                  fn.nested_in, fn.escapes, fn.returns_callable, fn.dynamic""",
        {"u": uid},
    ).result_set
    if not rows:
        return None
    r = rows[0]
    return {
        "uid": r[0], "qualname": r[1], "file": r[2], "lineno": r[3], "end_lineno": r[4],
        "decorators": r[5] or "", "is_public": bool(r[6]), "is_test": bool(r[7]),
        "last_modified": r[8], "doc": r[9],
        "nested_in": r[10] or "", "escapes": bool(r[11]), "returns_callable": bool(r[12]),
        "dynamic": r[13] or "",
    }


def file_exists(path: str) -> bool:
    return bool(run_query("file_exists", "MATCH (f:File {path: $p}) RETURN f.path", {"p": path}).result_set)


# ------------------------------------------------------------------ blast radius

def blast_radius(uid: str, depth: int = 3) -> dict | None:
    """Everything that depends on this function, directly or through other functions."""
    depth = max(1, min(int(depth), MAX_DEPTH))
    fn = function_details(uid)
    if not fn:
        return None

    rows = run_query(
        "impact_of",
        f"""MATCH p = (c:Function)-[:CALLS|REFERENCES*1..{depth}]->(t:Function {{uid: $uid}})
            RETURN c.uid, c.qualname, c.file, c.is_test, min(length(p)) AS hops
            ORDER BY hops, c.file LIMIT 300""",
        {"uid": uid},
    ).result_set
    callers = [
        {"uid": r[0], "qualname": r[1], "file": r[2], "is_test": bool(r[3]), "hops": r[4]} for r in rows
    ]

    # A method is also affected wherever its class is created (calls to User(...) are not calls to a function)
    instantiators, cls_name = [], None
    if "." in fn["qualname"]:
        cls_name = fn["qualname"].rsplit(".", 1)[0]
        rows = run_query(
            "impact_of",
            """MATCH (c:Function)-[:INSTANTIATES]->(k:Class {uid: $k})
               RETURN c.uid, c.qualname, c.file, c.is_test""",
            {"k": f"{fn['file']}::{cls_name}"},
        ).result_set
        instantiators = [
            {"uid": r[0], "qualname": r[1], "file": r[2], "is_test": bool(r[3])} for r in rows
        ]

    importers = [
        {"path": r[0], "is_test": bool(r[1])}
        for r in run_query(
            "impact_of",
            "MATCH (f:File)-[:IMPORTS]->(:File {path: $p}) RETURN f.path, f.is_test",
            {"p": fn["file"]},
        ).result_set
    ]

    prod_callers = [c for c in callers if not c["is_test"]]
    prod_inst = [i for i in instantiators if not i["is_test"]]
    test_hits = [c for c in callers if c["is_test"]] + [i for i in instantiators if i["is_test"]]
    return {
        "target": fn,
        "depth": depth,
        "callers": callers,
        "prod_callers": prod_callers,
        "instantiators": instantiators,
        "class_name": cls_name,
        "importers": importers,
        "tests": test_hits,
        # files OTHER than the target's own file that contain dependents
        "impacted_files": sorted(
            ({c["file"] for c in prod_callers} | {i["file"] for i in prod_inst}) - {fn["file"]}
        ),
    }


def class_radius(class_uid: str) -> dict:
    """Who creates or extends this class (used when a diff changes a class body)."""
    inst = run_query(
        "impact_of",
        "MATCH (c:Function)-[:INSTANTIATES]->(k:Class {uid: $k}) RETURN c.qualname, c.file, c.is_test",
        {"k": class_uid},
    ).result_set
    subs = run_query(
        "impact_of",
        "MATCH (s:Class)-[:INHERITS*1..3]->(k:Class {uid: $k}) RETURN DISTINCT s.qualname, s.file",
        {"k": class_uid},
    ).result_set
    return {
        "instantiators": [{"qualname": r[0], "file": r[1], "is_test": bool(r[2])} for r in inst],
        "subclasses": [{"qualname": r[0], "file": r[1]} for r in subs],
    }


def trace_flow(uid: str, depth: int = 3) -> list[dict]:
    """What this function calls, and what those call (forward direction)."""
    depth = max(1, min(int(depth), MAX_DEPTH))
    rows = run_query(
        "trace_flow",
        f"""MATCH p = (s:Function {{uid: $uid}})-[:CALLS*1..{depth}]->(c:Function)
            RETURN c.qualname, c.file, min(length(p)) AS hops
            ORDER BY hops, c.file LIMIT 40""",
        {"uid": uid},
    ).result_set
    return [{"qualname": r[0], "file": r[1], "hops": r[2]} for r in rows]


def key_files(limit: int = 8) -> list[dict]:
    """Most-imported non-test files: a cheap, useful 'read these first' list."""
    rows = run_query(
        "key_files",
        f"""MATCH (f:File)<-[:IMPORTS]-(i:File)
            WHERE f.is_test = false
            RETURN f.path, count(DISTINCT i) AS importers
            ORDER BY importers DESC LIMIT {int(limit)}""",
    ).result_set
    return [{"path": r[0], "importers": r[1]} for r in rows]


# ------------------------------------------------------------------ hidden coupling

def static_neighbors(path: str) -> set[str]:
    """Files already connected to this one in code (imports, calls, class use, inheritance)."""
    queries = (
        "MATCH (f:File {path: $p})-[:IMPORTS]-(g:File) RETURN DISTINCT g.path",
        "MATCH (a:Function {file: $p})-[:CALLS|REFERENCES]-(b:Function) RETURN DISTINCT b.file",
        "MATCH (a:Function {file: $p})-[:INSTANTIATES]->(k:Class) RETURN DISTINCT k.file",
        "MATCH (c:Function)-[:INSTANTIATES]->(k:Class {file: $p}) RETURN DISTINCT c.file",
        "MATCH (a:Class {file: $p})-[:INHERITS]-(b:Class) RETURN DISTINCT b.file",
    )
    out = set()
    for q in queries:
        for row in run_query("hidden_coupling", q, {"p": path}).result_set:
            out.add(row[0])
    out.discard(path)
    return out


def hidden_coupling(path: str, min_together: int = 2, min_ratio: float = 0.25, limit: int = 6) -> dict:
    """Files that keep changing in the same commits as `path`.

    `hidden` = the ones with NO code link to it (configs, templates, SQL, docs, tests that go
    through another route). Static analysis cannot see these; git history can.
    """
    total_rows = run_query(
        "hidden_coupling",
        "MATCH (c:Commit)-[:MODIFIED]->(f:File {path: $p}) WHERE c.n_files <= $mx RETURN count(c)",
        {"p": path, "mx": COCHANGE_MAX_FILES},
    ).result_set
    total = total_rows[0][0] if total_rows else 0
    if total < 3:
        return {"total": total, "partners": [], "hidden": []}

    rows = run_query(
        "hidden_coupling",
        """MATCH (f:File {path: $p})<-[:MODIFIED]-(c:Commit)-[:MODIFIED]->(g)
           WHERE g.path <> $p AND c.n_files <= $mx
           RETURN g.path, labels(g)[0], g.kind, g.is_test, count(DISTINCT c) AS together
           ORDER BY together DESC LIMIT 60""",
        {"p": path, "mx": COCHANGE_MAX_FILES},
    ).result_set
    neighbors = static_neighbors(path)
    partners = []
    for gpath, label, kind, is_test, together in rows:
        ratio = together / total
        if together < min_together or ratio < min_ratio:
            continue
        category = ("test" if is_test else "code") if label == "File" else (kind or "other")
        partners.append(
            {"path": gpath, "kind": category, "together": together, "ratio": round(ratio, 2), "linked": gpath in neighbors}
        )
    return {"total": total, "partners": partners, "hidden": [p for p in partners if not p["linked"]][:limit]}


# ------------------------------------------------------------------ repo text search

TEXT_SUFFIXES = (".py", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".md", ".rst", ".txt",
                 ".html", ".jinja", ".j2", ".sql", ".sh", ".env", ".conf")


@lru_cache(maxsize=1)
def _repo_files(root: str) -> tuple:
    out = []
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in config.IGNORE_DIRS and not d.endswith(".egg-info")]
        for fname in filenames:
            if fname.lower().endswith(TEXT_SUFFIXES):
                full = Path(dirpath) / fname
                try:
                    if full.stat().st_size < 1_000_000:
                        out.append(full)
                except OSError:
                    pass
        if len(out) > 4000:
            break
    return tuple(out)


@lru_cache(maxsize=256)
def _read_lines(root: str, rel: str) -> tuple:
    try:
        return tuple((Path(root) / rel).read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return ()


def source_line(rel: str, line: int) -> str | None:
    """The text of file:line, when the repo is available locally."""
    root = config.DEMO_REPO_PATH
    if not root:
        return None
    lines = _read_lines(str(root), rel)
    return lines[line - 1].strip()[:120] if 0 < line <= len(lines) else None


def string_mentions(name: str, limit: int = 12) -> list[dict] | None:
    """Mentions of a function name that are NOT calls: configs, docs, and string literals in code.

    This is where string-based wiring hides (entry points, registries, getattr(module, "name")).
    Returns None when the repo is not available locally.
    """
    root = config.DEMO_REPO_PATH
    if not root or not Path(root).is_dir():
        return None
    word = re.compile(r"\b" + re.escape(name) + r"\b")
    quoted = re.compile(r"""["'][^"'\n]*\b""" + re.escape(name) + r"""\b[^"'\n]*["']""")
    hits = []
    for full in _repo_files(str(root)):
        rel = full.relative_to(root).as_posix()
        is_py = rel.endswith(".py")
        for i, text in enumerate(_read_lines(str(root), rel), 1):
            if (quoted if is_py else word).search(text):
                hits.append({"file": rel, "line": i, "text": text.strip()[:120], "kind": "string literal" if is_py else "non-code file"})
                if len(hits) >= limit:
                    return hits
    return hits


# ------------------------------------------------------------------ blind spots

def blind_spots(fn: dict, br: dict) -> list[dict]:
    """Specific reasons THIS function's dependents might be incomplete (not a generic disclaimer)."""
    spots = []
    name = fn["qualname"].rsplit(".", 1)[-1]

    if fn["escapes"]:
        spots.append({"kind": "escapes", "text": (
            f"`{fn['qualname']}` is defined inside `{fn['nested_in']}` and returned as a value, so callers "
            f"hold it in a variable (x = {fn['nested_in'].rsplit('.', 1)[-1]}(...); x(...)). Those calls are "
            "invisible here: real callers are probably missing.")})
    if fn["returns_callable"]:
        spots.append({"kind": "returns_callable", "text": (
            f"`{fn['qualname']}` returns functions as values; whatever calls it receives callables whose "
            "later calls static analysis cannot follow.")})
    if fn["decorators"]:
        spots.append({"kind": "decorated", "text": (
            f"decorated with @{fn['decorators'].split(',')[0]}: a framework or the decorator itself may "
            "call it (routes, handlers, fixtures), which is not a visible call.")})
    if name.startswith("__") and name.endswith("__"):
        spots.append({"kind": "dunder", "text": "special method: Python calls it implicitly (construction, printing, comparison)."})
    if fn["is_public"] and not br["callers"] and not fn["is_test"] and not spots:
        spots.append({"kind": "public_unused", "text": "public function with no visible callers: it may be an entry point or used from outside the repo."})
    if fn["dynamic"]:
        spots.append({"kind": "dynamic_self", "text": f"it uses {fn['dynamic']}, so it may call or look up other code by name."})

    caller_uids = [c["uid"] for c in br["callers"]][:200]
    if caller_uids:
        rows = run_query(
            "blind_spots",
            "MATCH (f:Function) WHERE f.uid IN $uids AND f.dynamic <> '' RETURN f.qualname, f.dynamic LIMIT 3",
            {"uids": caller_uids},
        ).result_set
        if rows:
            spots.append({"kind": "dynamic_callers", "text": (
                "dependents that use dynamic features and may reach it indirectly: "
                + "; ".join(f"{q} ({d})" for q, d in rows))})
    rows = run_query(
        "blind_spots",
        "MATCH (f:Function {file: $p}) WHERE f.dynamic <> '' AND f.uid <> $u RETURN f.qualname, f.dynamic LIMIT 3",
        {"p": fn["file"], "u": fn["uid"]},
    ).result_set
    if rows:
        spots.append({"kind": "dynamic_file", "text": (
            f"other code in {fn['file']} uses dynamic features and could look it up by name: "
            + "; ".join(f"{q} ({d})" for q, d in rows))})

    rows = run_query(
        "blind_spots", "MATCH (f:Function {name: $n}) WHERE f.uid <> $u RETURN f.uid LIMIT 4", {"n": name, "u": fn["uid"]}
    ).result_set
    if rows:
        spots.append({"kind": "name_collision", "text": (
            f"{len(rows)}{'+' if len(rows) == 4 else ''} other function(s) are also named `{name}` "
            f"({', '.join(r[0] for r in rows[:3])}); calls matched by name may be mixed up between them.")})

    mentions = string_mentions(name, limit=5)
    if mentions:
        where = "; ".join(f"{m['file']}:{m['line']}" for m in mentions[:3])
        spots.append({"kind": "string_mentions", "text": (
            f"`{name}` also appears as text outside normal calls ({len(mentions)}{'+' if len(mentions) == 5 else ''} place(s), "
            f"e.g. {where}): string-based wiring that static analysis cannot follow.")})
    return spots


# ------------------------------------------------------------------ update checklist

def update_checklist(uid: str) -> dict | None:
    """Everything to touch if this function is removed or renamed, with file:line."""
    fn = function_details(uid)
    if not fn:
        return None
    name = fn["qualname"].rsplit(".", 1)[-1]

    rows = run_query(
        "update_checklist",
        """MATCH (c:Function)-[e:CALLS|REFERENCES]->(t:Function {uid: $u})
           RETURN c.qualname, c.file, c.is_test, type(e), e.sites
           ORDER BY c.is_test, c.file, c.qualname""",
        {"u": uid},
    ).result_set
    sites = []
    for qual, file, is_test, rel, raw in rows:
        for ln in [int(x) for x in (raw or "").split(",") if x.strip().isdigit()]:
            sites.append({
                "file": file, "line": ln, "in": qual, "is_test": bool(is_test),
                "how": "passed as a value (callback)" if rel == "REFERENCES" else "call",
                "text": source_line(file, ln),
            })

    imports = []
    for path, is_test, names, lines in run_query(
        "update_checklist",
        "MATCH (f:File)-[e:IMPORTS]->(t:File {path: $p}) RETURN f.path, f.is_test, e.names, e.lines",
        {"p": fn["file"]},
    ).result_set:
        if name in (names or "").split(","):
            for ln in [int(x) for x in (lines or "").split(",") if x.strip().isdigit()]:
                imports.append({"file": path, "line": ln, "is_test": bool(is_test), "text": source_line(path, ln)})

    return {
        "target": fn,
        "name": name,
        "sites": sites,
        "imports": imports,
        "mentions": string_mentions(name),   # None if the repo is not available locally
    }


# ------------------------------------------------------------------ people

def owners(uid: str | None = None, path: str | None = None) -> dict:
    """Who owns the code today (blame) and who has been active on the file (history)."""
    rows, level = [], None
    if uid:
        fn = function_details(uid)
        if fn:
            path = path or fn["file"]
            rows = run_query(
                "who_to_ask",
                """MATCH (a:Author)-[o:OWNS]->(fn:Function {uid: $u})
                   RETURN a.name, a.email, o.lines, o.share ORDER BY o.lines DESC LIMIT 3""",
                {"u": uid},
            ).result_set
            level = "function"
    if not rows and path:
        rows = run_query(
            "who_to_ask",
            """MATCH (a:Author)-[o:OWNS]->(f:File {path: $p})
               RETURN a.name, a.email, o.lines, o.share ORDER BY o.lines DESC LIMIT 3""",
            {"p": path},
        ).result_set
        level = "file"

    active = []
    if path:
        active = run_query(
            "who_to_ask",
            """MATCH (a:Author)-[:AUTHORED]->(c:Commit)-[:MODIFIED]->(f:File {path: $p})
               RETURN a.name, a.email, count(c) AS commits, max(c.ts) AS last
               ORDER BY commits DESC, last DESC LIMIT 3""",
            {"p": path},
        ).result_set
    return {
        "level": level,
        "path": path,
        "owners": [{"name": r[0], "email": r[1], "lines": r[2], "share": r[3]} for r in rows],
        "active": [{"name": r[0], "email": r[1], "commits": r[2], "last": r[3]} for r in active],
    }


def suggest_reviewers(uid: str, br: dict, limit: int = 3) -> list[dict]:
    """Rank people by how much of the target and of its dependents they wrote."""
    score: Counter = Counter()
    why: dict[str, list[str]] = defaultdict(list)
    names: dict[str, str] = {}

    own = owners(uid=uid)
    for o in own["owners"]:
        names[o["email"]] = o["name"]
        score[o["email"]] += 3 * (o["share"] or 0)
        why[o["email"]].append(f"wrote {round((o['share'] or 0) * 100)}% of the {own['level']} being changed")

    own_file = br["target"]["file"]
    rows = run_query(
        "who_to_ask",
        "MATCH (a:Author)-[o:OWNS]->(f:File {path: $p}) RETURN a.name, a.email, o.share",
        {"p": own_file},
    ).result_set
    for name, email, share in rows:
        names[email] = name
        score[email] += 1.5 * (share or 0)
        why[email].append(f"owns {round((share or 0) * 100)}% of {own_file} (where the target lives)")

    paths = br["impacted_files"][:10]
    if paths:
        rows = run_query(
            "who_to_ask",
            """MATCH (a:Author)-[o:OWNS]->(f:File) WHERE f.path IN $paths
               RETURN a.name, a.email, f.path, o.share""",
            {"paths": paths},
        ).result_set
        for name, email, path, share in rows:
            names[email] = name
            score[email] += share or 0
            why[email].append(f"owns {round((share or 0) * 100)}% of {path} (depends on the target)")

    return [
        {"name": names[email], "email": email, "reasons": why[email][:3]}
        for email, _ in score.most_common(limit)
    ]


# ------------------------------------------------------------------ risk

def _newest_commit_ts() -> int | None:
    rows = run_query("risk", "MATCH (c:Commit) RETURN max(c.ts)").result_set
    return rows[0][0] if rows and rows[0][0] else None


def recent_churn(path: str, days: int = 90) -> int:
    """Commits touching the file in the `days` before the repo's newest commit."""
    newest = _newest_commit_ts()
    if not newest:
        return 0
    rows = run_query(
        "risk",
        "MATCH (c:Commit)-[:MODIFIED]->(f:File {path: $p}) WHERE c.ts >= $since RETURN count(c)",
        {"p": path, "since": newest - days * 86400},
    ).result_set
    return rows[0][0] if rows else 0


def risk_report(uid: str, depth: int = 3) -> dict | None:
    """Blast radius + owners + a risk level made of visible, individually explained factors."""
    br = blast_radius(uid, depth)
    if not br:
        return None
    fn = br["target"]
    prod_direct = [c for c in br["prod_callers"] if c["hops"] == 1]
    prod_inst = [i for i in br["instantiators"] if not i["is_test"]]
    n_direct = len(prod_direct) + len(prod_inst)

    factors = []

    pts = 0 if n_direct == 0 else 1 if n_direct <= 3 else 2 if n_direct <= 10 else 3
    factors.append({"factor": "Direct dependents", "detail": f"{n_direct} production function(s) call or instantiate it", "points": pts})

    n_files = len(br["impacted_files"])
    pts = 2 if n_files >= 15 else 1 if n_files >= 6 else 0
    factors.append({"factor": "Reach", "detail": f"affects {n_files} other file(s) within {br['depth']} hops", "points": pts})

    if not fn["is_test"]:
        if br["tests"]:
            files = sorted({t["file"] for t in br["tests"]})
            factors.append({"factor": "Test coverage (static)", "detail": f"reached by {len(br['tests'])} test function(s) in {len(files)} file(s)", "points": 0})
        else:
            factors.append({"factor": "Test coverage (static)", "detail": "no test calls it, directly or indirectly, that static analysis can see", "points": 2})

    spots = blind_spots(fn, br)
    visible_kinds = {"escapes", "returns_callable", "decorated", "dunder", "public_unused"}
    flagged = [sp for sp in spots if sp["kind"] in visible_kinds]
    factors.append({
        "factor": "Possible hidden callers",
        "detail": flagged[0]["text"] if flagged else "none detected by the parser",
        "points": 1 if flagged else 0,
    })

    own = owners(uid=uid)
    file_top = run_query(
        "risk",
        """MATCH (a:Author)-[o:OWNS]->(f:File {path: $p})
           RETURN a.name, o.share ORDER BY o.lines DESC LIMIT 1""",
        {"p": fn["file"]},
    ).result_set
    if file_top and (file_top[0][1] or 0) >= 0.8:
        factors.append({"factor": "Knowledge concentration", "detail": f"{file_top[0][0]} wrote {round(file_top[0][1] * 100)}% of {fn['file']}", "points": 1})
    else:
        factors.append({"factor": "Knowledge concentration", "detail": "ownership is shared or unknown", "points": 0})

    churn = recent_churn(fn["file"])
    factors.append({"factor": "Recent churn", "detail": f"{churn} commit(s) touched {fn['file']} in the 90 days before the newest commit", "points": 1 if churn >= 5 else 0})

    hidden = hidden_coupling(fn["file"])
    strong = [h for h in hidden["hidden"] if h["together"] >= 3 and h["ratio"] >= 0.4]
    if strong:
        top = ", ".join(f"{h['path']} ({h['together']}/{hidden['total']} commits)" for h in strong[:2])
        factors.append({"factor": "Hidden coupling", "detail": f"usually changes together with code it has no link to: {top}", "points": 1})
    else:
        factors.append({"factor": "Hidden coupling", "detail": "no strongly co-changing unlinked files", "points": 0})

    static_tests = sorted({t["file"] for t in br["tests"]})
    cochanged_tests = [h["path"] for h in hidden["partners"] if h["kind"] == "test" and h["path"] not in static_tests]

    score = sum(f["points"] for f in factors)
    level = "LOW" if score <= 2 else "MEDIUM" if score <= 5 else "HIGH"
    return {
        "blast": br,
        "owners": own,
        "reviewers": suggest_reviewers(uid, br),
        "factors": factors,
        "score": score,
        "level": level,
        "hidden": hidden,
        "blind_spots": spots,
        "tests_to_run": {"static": static_tests, "co_changed": cochanged_tests},
    }


# ------------------------------------------------------------------ diffs

def map_diff(diff_text: str) -> list[dict]:
    """Map a unified diff to the functions/classes it touches in the graph.

    The graph holds the code as it is BEFORE the change, so we use old-file line
    numbers (removed lines, and the line an addition was inserted after).
    """
    from unidiff import PatchSet

    patch = PatchSet(diff_text.strip("\n") + "\n")
    changes = []
    for pf in patch:
        path = pf.path
        if not path.endswith(".py"):
            continue
        entry = {
            "file": path,
            "new_file": pf.is_added_file,
            "removed_file": pf.is_removed_file,
            "known": file_exists(path),
            "functions": {},
            "classes": {},
            "module_level": False,
        }
        if entry["new_file"] or not entry["known"]:
            changes.append(entry)
            continue

        funcs = run_query(
            "map_diff",
            "MATCH (fn:Function {file: $f}) RETURN fn.uid, fn.qualname, fn.lineno, fn.end_lineno, fn.indent",
            {"f": path},
        ).result_set
        classes = run_query(
            "map_diff",
            "MATCH (c:Class {file: $f}) RETURN c.uid, c.qualname, c.lineno, c.end_lineno, c.indent",
            {"f": path},
        ).result_set

        if entry["removed_file"]:
            for uid, q, _, _, _ in funcs:
                entry["functions"][uid] = q
            changes.append(entry)
            continue

        # Each touched spot is (old line, indent of the changed text, text, next old line).
        # indent=None means a line that existed before (removed): inside any range covering it.
        spots = []
        for hunk in pf:
            last_old = hunk.source_start
            anchor = hunk.source_start  # last old line with real text (git may park additions after blank lines)
            new_def_indent = None  # indent of an added def/class whose body we are currently inside
            for line in hunk:
                text = line.value.rstrip("\r\n")
                if line.is_context:
                    last_old = line.source_line_no
                    if text.strip():
                        anchor = line.source_line_no
                    new_def_indent = None
                elif line.is_removed:
                    last_old = line.source_line_no
                    if text.strip():
                        anchor = line.source_line_no
                    new_def_indent = None
                    if text.strip():
                        spots.append((line.source_line_no, None, text, line.source_line_no + 1))
                elif line.is_added and text.strip():
                    indent = len(text) - len(text.lstrip())
                    if new_def_indent is not None and indent > new_def_indent:
                        continue  # body of a function/class that is itself newly added
                    new_def_indent = None
                    if text.lstrip().startswith(("def ", "async def ", "class ")):
                        new_def_indent = indent
                    # an addition sits between old lines last_old and last_old+1; whether it is
                    # INSIDE a function depends on how far it is indented, not just on position
                    spots.append((anchor, indent, text, last_old + 1))

        entry["new_definitions"] = []
        for ln, indent, text, next_ln in spots:
            if indent is not None and text.lstrip().startswith("@"):
                # an added decorator belongs to the def right below it (the next old line)
                hits = [
                    (end - start, uid, q)
                    for uid, q, start, end, ind in funcs
                    if start <= next_ln <= end and (ind or 0) == indent
                ]
                if hits:
                    _, uid, q = min(hits)
                    entry["functions"][uid] = q
                continue  # otherwise it decorates a brand-new def: nothing existing changed
            fn_hits = [
                (end - start, uid, q)
                for uid, q, start, end, ind in funcs
                if start <= ln <= end and (indent is None or indent > (ind or 0))
            ]
            if fn_hits:
                _, uid, q = min(fn_hits)  # innermost function wins
                entry["functions"][uid] = q
                continue
            cls_hits = [
                (end - start, uid, q)
                for uid, q, start, end, ind in classes
                if start <= ln <= end and (indent is None or indent > (ind or 0))
            ]
            if cls_hits:
                _, uid, q = min(cls_hits)
                entry["classes"][uid] = q
            elif indent is not None and text.lstrip().startswith(("def ", "async def ", "class ")):
                entry["new_definitions"].append(text.strip().split("(")[0][:60])
            else:
                entry["module_level"] = True
        changes.append(entry)
    return changes


def analyze_diff(diff_text: str, depth: int = 3, max_functions: int = 8) -> dict:
    """Run the full risk report for every function a diff touches, then combine."""
    try:
        changes = map_diff(diff_text)
    except Exception as exc:  # malformed diff
        return {"error": f"Could not parse the diff: {exc}"}
    if not changes:
        return {"error": "No Python file changes found in the diff."}

    reports, class_changes, module_changes, new_files, unknown = [], [], [], [], []
    new_defs = [(ch["file"], d) for ch in changes for d in ch.get("new_definitions", [])]
    seen = set()
    for ch in changes:
        if ch["new_file"]:
            new_files.append(ch["file"])
            continue
        if not ch["known"]:
            unknown.append(ch["file"])
            continue
        for uid in ch["functions"]:
            if uid not in seen and len(seen) < max_functions:
                seen.add(uid)
                rep = risk_report(uid, depth)
                if rep:
                    reports.append(rep)
        for uid, q in ch["classes"].items():
            class_changes.append({"qualname": q, "file": ch["file"], **class_radius(uid)})
        if ch["module_level"]:
            importers = [
                r[0]
                for r in run_query(
                    "analyze_diff",
                    "MATCH (f:File)-[:IMPORTS]->(:File {path: $p}) RETURN f.path",
                    {"p": ch["file"]},
                ).result_set
            ]
            module_changes.append({"file": ch["file"], "importers": importers})

    changed_paths = {ch["file"] for ch in changes}
    try:  # every file in the diff counts as "changed", including configs and docs
        from unidiff import PatchSet

        changed_paths |= {pf.path for pf in PatchSet(diff_text.strip("\n") + "\n")}
    except Exception:
        pass
    missing = []
    for ch in changes[:6]:
        if ch["new_file"] or not ch["known"]:
            continue
        hc = hidden_coupling(ch["file"])
        for h in hc["hidden"]:
            if h["path"] not in changed_paths:
                missing.append({"changed": ch["file"], "total": hc["total"], **h})

    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    overall = max((r["level"] for r in reports), key=order.get, default="LOW")
    if class_changes and overall == "LOW" and any(c["instantiators"] or c["subclasses"] for c in class_changes):
        overall = "MEDIUM"

    tally: Counter = Counter()
    info: dict[str, dict] = {}
    for rep in reports:
        for rv in rep["reviewers"]:
            tally[rv["email"]] += 1
            info.setdefault(rv["email"], rv)
    return {
        "changes": changes,
        "reports": reports,
        "class_changes": class_changes,
        "module_changes": module_changes,
        "new_files": new_files,
        "new_definitions": new_defs,
        "missing_cochanged": missing[:10],
        "unknown_files": unknown,
        "overall": overall,
        "reviewers": [info[e] for e, _ in tally.most_common(3)],
        "truncated": sum(len(c["functions"]) for c in changes) > max_functions,
    }
