"""Strands tools: thin wrappers that turn graph analysis into compact text for the LLM.

The heavy lifting lives in analysis.py. Each tool here resolves a name, calls the
analysis, and formats the result. Docstrings matter: the model reads them to
decide which tool to call, so they say WHEN to use each one.
"""
import time

from strands import tool

from agent import analysis as A

# The UI / CLI stores a pasted diff here so the model never has to copy a long diff into a tool call.
_current = {"diff": ""}


def set_current_diff(text: str) -> None:
    _current["diff"] = text or ""


def has_current_diff() -> bool:
    return bool(_current["diff"].strip())


# ------------------------------------------------------------------ helpers

def _where(f: dict) -> str:
    loc = f":{f['lineno']}" if f.get("lineno") else ""
    return f"{f['qualname']} ({f['file']}{loc})"


def resolve_target(target: str) -> tuple[str | None, str | None]:
    """Return (uid, None) when exactly one function matches, else (None, helpful message)."""
    t = target.strip()
    matches = A.lookup_functions(t)
    if len(matches) == 1:
        return matches[0]["uid"], None
    if not matches:
        similar = A.search_functions(t.rsplit(".", 1)[-1], 5)
        if similar:
            opts = "; ".join(m["uid"] for m in similar)
            return None, f"No function exactly named '{t}'. Similar: {opts}. Call again with one of these uids."
        return None, f"No function named '{t}' in the graph. Try find_functions with part of the name."
    opts = "; ".join(m["uid"] for m in matches)
    return None, f"'{t}' is ambiguous ({len(matches)} matches): {opts}. Call again with the full uid."


def _date(ts) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "unknown"


def _people(rows: list[dict], with_commits: bool = False) -> str:
    if not rows:
        return "none found"
    if with_commits:
        return "; ".join(f"{r['name']} ({r['commits']} commits, last {_date(r['last'])})" for r in rows)
    return "; ".join(f"{r['name']} ({round((r['share'] or 0) * 100)}% / {r['lines']} lines)" for r in rows)


def _blast_text(br: dict, limit: int = 10) -> str:
    out = []
    prod = br["prod_callers"]
    out.append(f"Known dependents within {br['depth']} hops: {len(prod)} production function(s)")
    for c in prod[:limit]:
        out.append(f"  - hop {c['hops']}: {c['qualname']} ({c['file']})")
    if len(prod) > limit:
        out.append(f"  ... and {len(prod) - limit} more")
    if br["instantiators"]:
        inst = [i for i in br["instantiators"] if not i["is_test"]]
        out.append(f"Code that creates class {br['class_name']} (affected if constructor/state changes): {len(inst)}")
        for i in inst[:5]:
            out.append(f"  - {i['qualname']} ({i['file']})")
    if br["importers"]:
        prod_imp = [i["path"] for i in br["importers"] if not i["is_test"]]
        out.append(f"Files importing {br['target']['file']}: {len(prod_imp)}" + (f" ({', '.join(prod_imp[:6])})" if prod_imp else ""))
    out.append(f"Other files containing dependents: {len(br['impacted_files'])}")
    return "\n".join(out)


def _coupling_text(hidden: dict, limit: int = 5) -> list[str]:
    if not hidden["hidden"]:
        return ["  none: nothing that keeps changing with this file lacks a code link to it"]
    return [
        f"  - {h['path']} [{h['kind']}]: changed in {h['together']} of {hidden['total']} commits that touched this file"
        for h in hidden["hidden"][:limit]
    ]


# ------------------------------------------------------------------ tools

@tool
def find_functions(query: str) -> str:
    """Search for functions or methods by (part of) their name.

    Use this FIRST when the user mentions a function loosely, or when another tool
    says a name is ambiguous or not found. Returns qualified names, files and uids.

    Args:
        query: Part of a function or method name, e.g. "save" or "User.save".
    """
    rows = A.search_functions(query, 12)
    if not rows:
        return f"No functions matching '{query}'."
    return "Matches (use the uid to be exact):\n" + "\n".join(f"- {r['qualname']}  [{r['uid']}]" for r in rows)


@tool
def risk_report(target: str, depth: int = 3) -> str:
    """Full change-risk report for one function: risk level with every contributing factor,
    blast radius, tests to run, files that secretly change together with it, blind spots
    specific to this function, and who should review the change.

    Use for ANY question about changing, removing, renaming, deleting or no longer using a
    function ("is it safe?", "what breaks?", "what if I remove X?"). Call this before giving a verdict.

    Args:
        target: Function or method name (e.g. "save_user", "User.save") or a full uid.
        depth: How many call hops to follow, 1 to 4 (default 3).
    """
    uid, msg = resolve_target(target)
    if msg:
        return msg
    rep = A.risk_report(uid, depth)
    br = rep["blast"]
    tests = rep["tests_to_run"]

    lines = [f"RISK: {rep['level']} (score {rep['score']}) for {_where(br['target'])}", "Factors:"]
    lines += [f"  +{f['points']} {f['factor']}: {f['detail']}" for f in rep["factors"]]
    lines += ["", _blast_text(br), "", "Tests to run:"]
    lines.append("  reach it through code: " + (", ".join(tests["static"]) or "none found"))
    if tests["co_changed"]:
        lines.append("  usually change with this file (no code link): " + ", ".join(tests["co_changed"]))
    lines += ["", f"Hidden coupling (files that keep changing with {br['target']['file']} but have no code link to it):"]
    lines += _coupling_text(rep["hidden"])
    lines += ["", "Blind spots specific to this function:"]
    if rep["blind_spots"]:
        lines += [f"  - {b['text']}" for b in rep["blind_spots"]]
    else:
        lines.append("  none detected by the parser (code outside this repo is still invisible)")
    lines += ["", "Suggested reviewers:"]
    for r in rep["reviewers"] or []:
        lines.append(f"  - {r['name']}: " + "; ".join(r["reasons"]))
    if not rep["reviewers"]:
        lines.append("  none found (no git ownership data loaded?)")
    lines.append(f"Recently active on {br['target']['file']}: {_people(rep['owners']['active'], with_commits=True)}")
    return "\n".join(lines)


@tool
def update_checklist(target: str) -> str:
    """Checklist of everything to update if a function is removed or renamed, with file:line
    for each call site, callback use and explicit import, plus text mentions outside normal calls.

    Use for "what do I need to change if I rename/remove/delete X?" and "where is X used?".

    Args:
        target: Function or method name (e.g. "run_optimizer", "User.save") or a full uid.
    """
    uid, msg = resolve_target(target)
    if msg:
        return msg
    ck = A.update_checklist(uid)
    fn = ck["target"]
    prod = [s for s in ck["sites"] if not s["is_test"]]
    tests = [s for s in ck["sites"] if s["is_test"]]

    def fmt(s):
        text = f": `{s['text']}`" if s.get("text") else ""
        return f"  - {s['file']}:{s['line']} in {s['in']} ({s['how']}){text}"

    lines = [f"CHECKLIST for removing or renaming {_where(fn)}"]
    lines.append(f"Production call sites ({len(prod)}):")
    lines += [fmt(s) for s in prod[:20]] or ["  none found"]
    if len(prod) > 20:
        lines.append(f"  ... and {len(prod) - 20} more")
    lines.append(f"Test call sites ({len(tests)}) (these will fail):")
    lines += [fmt(s) for s in tests[:10]] or ["  none found"]
    lines.append(f"Explicit imports of `{ck['name']}` ({len(ck['imports'])}):")
    lines += [
        f"  - {i['file']}:{i['line']}" + (f": `{i['text']}`" if i.get("text") else "")
        for i in ck["imports"][:10]
    ] or ["  none found"]
    if ck["mentions"] is None:
        lines.append("Text mentions outside calls: not searched (the repo files are not available on this machine)")
    else:
        lines.append(f"Mentions outside normal calls ({len(ck['mentions'])}) (configs, docs, string literals):")
        lines += [f"  - {m['file']}:{m['line']} [{m['kind']}]: `{m['text']}`" for m in ck["mentions"][:10]] or ["  none found"]
    lines.append("Not detectable: calls through variables holding the function, and code outside this repo.")
    return "\n".join(lines)


@tool
def who_to_ask(target: str) -> str:
    """Find the right people to ask about a function or a file.

    Returns two views: who WROTE the code that exists today (git blame ownership) and who has
    been ACTIVE on the file recently (commit history).

    Args:
        target: A function name/uid (e.g. "User.save") or a file path (e.g. "app/models.py").
    """
    t = target.strip()
    if "::" not in t and (t.endswith(".py") or "/" in t):  # a uid like "a/b.py::f" is a function
        if not A.file_exists(t):
            return f"No file '{t}' in the graph."
        res = A.owners(path=t)
        label = f"file {t}"
    else:
        uid, msg = resolve_target(t)
        if msg:
            return msg
        res = A.owners(uid=uid)
        label = f"{uid}"
    return (
        f"People for {label}\n"
        f"Wrote the code that exists now ({res['level']}-level blame): {_people(res['owners'])}\n"
        f"Most active recently on {res['path']}: {_people(res['active'], with_commits=True)}"
    )


@tool
def analyze_diff(diff_text: str = "CURRENT") -> str:
    """Analyze a code change (a unified diff, e.g. from `git diff` or a pull request).

    Maps the changed lines to the functions/classes they touch, then reports blast radius,
    tests, risk level, files that usually change together with the touched files but are
    MISSING from this diff, and suggested reviewers.

    Use whenever the user says a diff, patch, PR or set of changes is loaded or pasted.

    Args:
        diff_text: Pass "CURRENT" to analyze the diff the user has loaded (preferred, no need to
            copy it). Only pass raw diff text if the user pasted it directly into the chat.
    """
    text = _current["diff"] if diff_text.strip() in ("", "CURRENT") else diff_text
    if not text.strip():
        return "No diff is loaded. Ask the user to load or paste a diff."
    res = A.analyze_diff(text)
    if "error" in res:
        return res["error"]

    lines = [f"CHANGE ANALYSIS: overall risk {res['overall']}"]
    for ch in res["changes"]:
        bits = []
        if ch["new_file"]:
            bits.append("new file")
        elif not ch["known"]:
            bits.append("file not in the graph (new or not analyzed)")
        else:
            if ch["functions"]:
                bits.append("functions: " + ", ".join(sorted(ch["functions"].values())))
            if ch["classes"]:
                bits.append("class body: " + ", ".join(sorted(ch["classes"].values())))
            if ch["module_level"]:
                bits.append("module-level code")
            if ch.get("new_definitions"):
                bits.append("adds: " + ", ".join(ch["new_definitions"]))
        lines.append(f"- {ch['file']}: " + ("; ".join(bits) or "no code changes detected"))

    lines.append("")
    for rep in res["reports"]:
        br = rep["blast"]
        n_tests = len(rep["tests_to_run"]["static"])
        lines.append(
            f"* {br['target']['qualname']} [{rep['level']}, score {rep['score']}]: "
            f"{len(br['prod_callers'])} dependent function(s) in {len(br['impacted_files'])} other file(s), "
            f"{n_tests} test file(s) reach it"
        )
        top = [f for f in rep["factors"] if f["points"] > 0]
        if top:
            lines.append("    risk factors: " + "; ".join(f"{f['factor']} ({f['detail']})" for f in top))
        spots = [b["text"] for b in rep["blind_spots"] if b["kind"] in ("escapes", "returns_callable", "decorated", "string_mentions")]
        if spots:
            lines.append("    blind spot: " + spots[0])
    for cc in res["class_changes"]:
        lines.append(
            f"* class {cc['qualname']} body changed: created in {len(cc['instantiators'])} place(s), "
            f"{len(cc['subclasses'])} subclass(es)"
        )
    for mc in res["module_changes"]:
        lines.append(f"* module-level change in {mc['file']}: imported by {len(mc['importers'])} file(s)")

    if res["missing_cochanged"]:
        lines.append("")
        lines.append("Usually changes together with the touched files, but is NOT in this diff (check it):")
        for m in res["missing_cochanged"]:
            lines.append(f"  - {m['path']} [{m['kind']}]: changed in {m['together']} of {m['total']} commits touching {m['changed']}")

    all_tests = sorted({f for r in res["reports"] for f in r["tests_to_run"]["static"]})
    co_tests = sorted({f for r in res["reports"] for f in r["tests_to_run"]["co_changed"]} - set(all_tests))
    all_files = sorted({f for r in res["reports"] for f in r["blast"]["impacted_files"]})
    lines.append("")
    lines.append(f"Other files containing dependents: {len(all_files)}" + (f" ({', '.join(all_files[:8])})" if all_files else ""))
    lines.append("Tests to run: " + (", ".join(all_tests[:8]) if all_tests else "none found through code")
                 + (f"; also usually change with these files: {', '.join(co_tests[:5])}" if co_tests else ""))
    lines.append("Suggested reviewers:")
    for r in res["reviewers"] or []:
        lines.append(f"  - {r['name']}: " + "; ".join(r["reasons"]))
    if not res["reviewers"]:
        lines.append("  none found")
    if res["truncated"]:
        lines.append("(Only the first functions were analyzed in detail; ask for risk_report on a specific one.)")
    return "\n".join(lines)


@tool
def trace_flow(target: str, depth: int = 2) -> str:
    """Show what a function calls, and what those functions call (the forward flow).

    Use for onboarding questions like "what happens when X runs?" or "how does X work?".

    Args:
        target: Function or method name or a full uid.
        depth: How many hops to follow, 1 to 4 (default 2).
    """
    uid, msg = resolve_target(target)
    if msg:
        return msg
    rows = A.trace_flow(uid, depth)
    if not rows:
        return f"{uid} calls no other functions that were found in this repo."
    return f"Flow from {uid}:\n" + "\n".join(f"  - hop {r['hops']}: {r['qualname']} ({r['file']})" for r in rows)


@tool
def key_files(limit: int = 8) -> str:
    """List the most important files to read first, ranked by how many other files import them.

    Use for onboarding questions like "where should I start?" or "what are the core modules?".

    Args:
        limit: How many files to list (default 8).
    """
    rows = A.key_files(limit)
    if not rows:
        return "No import relationships found."
    return "Most-imported files (read these first):\n" + "\n".join(
        f"  - {r['path']}: imported by {r['importers']} file(s)" for r in rows
    )


ALL_TOOLS = [find_functions, risk_report, update_checklist, who_to_ask, analyze_diff, trace_flow, key_files]

