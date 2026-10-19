"""Reusable Streamlit renderers. Pure display: all the analysis happens in agent/analysis.py."""
import time

import pandas as pd
import streamlit as st

from agent import analysis as A
from UserInterface import graph_view, report


def level_banner(level: str, score: int, title: str) -> None:
    box = {"HIGH": st.error, "MEDIUM": st.warning, "LOW": st.success}.get(level, st.info)
    box(f"**{level} RISK** (score {score}): {title}")


def _date(ts) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "unknown"


def trace_panel(trace: list, evidence: str | None = None, used: list | None = None, key: str = "t") -> None:
    """'How I got this': which analyses ran, and the exact Cypher behind them.

    (Two sibling expanders, not nested ones: older Streamlit versions reject nesting.)
    """
    with st.expander("How I got this (graph queries)"):
        if used:
            st.markdown("**Analyses run:** " + ", ".join(f"`{u}`" for u in used))
        if trace:
            df = pd.DataFrame(
                [
                    {"step": t["tool"], "rows": t["rows"], "ms": t["ms"], "cypher": t["cypher"][:150]}
                    for t in trace
                ]
            )
            st.caption(f"{len(trace)} queries, {sum(t['ms'] for t in trace)} ms total")
            st.dataframe(df, hide_index=True)
            st.markdown("**Full Cypher of every query**")
            st.code(
                "\n\n".join(f"-- {t['tool']} ({t['rows']} rows, {t['ms']} ms)\n{t['cypher']}" for t in trace),
                language="sql",
            )
        else:
            st.caption("No graph queries were needed for this reply.")
    if evidence:
        with st.expander("Raw evidence the answer was written from"):
            st.code(evidence, language=None)


def factors_table(factors: list[dict]) -> None:
    df = pd.DataFrame(
        [{"factor": f["factor"], "points": f["points"], "detail": f["detail"]} for f in factors]
    )
    st.dataframe(df, hide_index=True)


def _hidden_table(hidden: dict) -> None:
    if not hidden["hidden"]:
        st.caption("Nothing keeps changing with this file that lacks a code link to it.")
        return
    df = pd.DataFrame(
        [
            {
                "file": h["path"],
                "kind": h["kind"],
                "changed together": f"{h['together']} of {hidden['total']} commits",
            }
            for h in hidden["hidden"]
        ]
    )
    st.dataframe(df, hide_index=True)


def render_function_report(state: dict, key: str) -> None:
    """The risk card for one function. `state` holds rep, dot, trace and an optional checklist."""
    rep = state["rep"]
    br = rep["blast"]
    t = br["target"]

    level_banner(rep["level"], rep["score"], f"`{t['qualname']}` in {t['file']}:{t['lineno']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Known dependents", len(br["prod_callers"]))
    c2.metric("Other files affected", len(br["impacted_files"]))
    c3.metric("Test files reaching it", len(rep["tests_to_run"]["static"]))
    c4.metric("Hidden-coupled files", len(rep["hidden"]["hidden"]))

    left, right = st.columns([3, 2])
    with left:
        st.markdown("##### Blast radius")
        st.graphviz_chart(state["dot"])
        st.caption(graph_view.LEGEND)
    with right:
        st.markdown("##### Who to ask / review")
        if rep["reviewers"]:
            for r in rep["reviewers"]:
                st.markdown(f"**{r['name']}**  \n" + "  \n".join(f"- {x}" for x in r["reasons"]))
        else:
            st.caption("No ownership data (run `ingest.mine_git`).")
        active = rep["owners"]["active"]
        if active:
            st.markdown(
                f"**Recently active on `{t['file']}`:** "
                + "; ".join(f"{a['name']} ({a['commits']} commits, last {_date(a['last'])})" for a in active)
            )

    st.markdown("##### Why this level")
    factors_table(rep["factors"])

    cols = st.columns(2)
    with cols[0]:
        st.markdown("##### Easy to miss: hidden coupling")
        st.caption(f"Files that keep changing in the same commits as `{t['file']}` but have no code link to it.")
        _hidden_table(rep["hidden"])
    with cols[1]:
        st.markdown("##### Blind spots specific to this function")
        if rep["blind_spots"]:
            for b in rep["blind_spots"]:
                st.markdown(f"- {b['text']}")
        else:
            st.caption("None detected by the parser. Code outside this repository is still invisible.")

    st.markdown("##### Tests to run")
    tests = rep["tests_to_run"]
    st.markdown("- reach it through code: " + (", ".join(f"`{x}`" for x in tests["static"]) or "none found"))
    if tests["co_changed"]:
        st.markdown("- usually change with this file (no code link): " + ", ".join(f"`{x}`" for x in tests["co_changed"]))

    # removal / rename checklist, computed on demand
    if st.checkbox("Show the removal / rename checklist (file:line)", key=f"ck_{key}"):
        if state.get("checklist") is None:
            state["checklist"] = A.update_checklist(t["uid"])
        render_checklist(state["checklist"])

    with st.expander("Copy-ready review comment (Markdown)"):
        comment = report.function_comment(rep)
        st.code(comment, language="markdown")
        st.download_button("Download as .md", comment, file_name=f"review_{t['qualname']}.md", key=f"dl_{key}")

    trace_panel(state.get("trace", []), key=key)


def render_checklist(ck: dict) -> None:
    prod = [s for s in ck["sites"] if not s["is_test"]]
    tests = [s for s in ck["sites"] if s["is_test"]]

    def line(s):
        txt = f": `{s['text']}`" if s.get("text") else ""
        return f"- `{s['file']}:{s['line']}` in `{s['in']}` ({s['how']}){txt}"

    st.markdown(f"**Production call sites ({len(prod)})**")
    st.markdown("\n".join(line(s) for s in prod[:25]) or "- none found")
    st.markdown(f"**Test call sites ({len(tests)})** (these will fail)")
    st.markdown("\n".join(line(s) for s in tests[:10]) or "- none found")
    st.markdown(f"**Explicit imports of `{ck['name']}` ({len(ck['imports'])})**")
    st.markdown(
        "\n".join(
            f"- `{i['file']}:{i['line']}`" + (f": `{i['text']}`" if i.get("text") else "") for i in ck["imports"][:10]
        )
        or "- none found"
    )
    if ck["mentions"] is None:
        st.caption("Text mentions outside normal calls were not searched: the repository files are not available on this machine.")
    else:
        st.markdown(f"**Mentions outside normal calls ({len(ck['mentions'])})**: configs, docs, string literals")
        st.markdown(
            "\n".join(f"- `{m['file']}:{m['line']}` [{m['kind']}]: `{m['text']}`" for m in ck["mentions"][:10])
            or "- none found"
        )
    st.caption("Not detectable: calls through variables that hold the function, and code outside this repository.")


def render_diff_result(state: dict) -> None:
    """The report for a whole change. `state` holds res, trace and per-function dots."""
    res = state["res"]
    if "error" in res:
        st.error(res["error"])
        return

    level_banner(res["overall"], sum(r["score"] for r in res["reports"]), "overall risk of this change")

    if res["reports"]:
        df = pd.DataFrame(
            [
                {
                    "function": r["blast"]["target"]["qualname"],
                    "file": r["blast"]["target"]["file"],
                    "risk": f"{r['level']} ({r['score']})",
                    "dependents": len(r["blast"]["prod_callers"]),
                    "other files": len(r["blast"]["impacted_files"]),
                    "test files": len(r["tests_to_run"]["static"]),
                }
                for r in res["reports"]
            ]
        )
        st.dataframe(df, hide_index=True)
    if res["truncated"]:
        st.caption("Only the first functions are analyzed in detail.")

    notes = []
    if res["new_files"]:
        notes.append("New files: " + ", ".join(f"`{f}`" for f in res["new_files"]))
    if res["new_definitions"]:
        notes.append("New definitions: " + ", ".join(f"`{d}` in {f}" for f, d in res["new_definitions"]))
    if res["unknown_files"]:
        notes.append("Not in the graph (new or not analyzed): " + ", ".join(f"`{f}`" for f in res["unknown_files"]))
    for cc in res["class_changes"]:
        notes.append(f"Class `{cc['qualname']}` body changed: created in {len(cc['instantiators'])} place(s), {len(cc['subclasses'])} subclass(es)")
    for mc in res["module_changes"]:
        notes.append(f"Module-level change in `{mc['file']}`: imported by {len(mc['importers'])} file(s)")
    for n in notes:
        st.markdown(f"- {n}")

    if res["missing_cochanged"]:
        st.warning("**Usually changes together with the touched files, but is NOT in this change. Check it:**")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "file": m["path"],
                        "kind": m["kind"],
                        "changed together": f"{m['together']} of {m['total']} commits",
                        "touching": m["changed"],
                    }
                    for m in res["missing_cochanged"]
                ]
            ),
            hide_index=True,
        )

    tests = sorted({f for r in res["reports"] for f in r["tests_to_run"]["static"]})
    co = sorted({f for r in res["reports"] for f in r["tests_to_run"]["co_changed"]} - set(tests))
    st.markdown("##### Tests to run")
    st.markdown("- reach the changed code: " + (", ".join(f"`{t}`" for t in tests) or "none found through code"))
    if co:
        st.markdown("- usually change with these files: " + ", ".join(f"`{t}`" for t in co))

    st.markdown("##### Suggested reviewers")
    if res["reviewers"]:
        for r in res["reviewers"]:
            st.markdown(f"**{r['name']}**: " + "; ".join(r["reasons"]))
    else:
        st.caption("No ownership data found.")

    if res["reports"]:
        st.markdown("##### Drill into one function")
        names = [f"{r['blast']['target']['qualname']}  ({r['blast']['target']['file']})" for r in res["reports"]]
        pick = st.selectbox("Function", range(len(names)), format_func=lambda i: names[i], key="diff_pick")
        rep = res["reports"][pick]
        left, right = st.columns([3, 2])
        with left:
            st.graphviz_chart(state["dots"][pick])
            st.caption(graph_view.LEGEND)
        with right:
            factors_table(rep["factors"])
            if rep["blind_spots"]:
                st.markdown("**Blind spots**")
                for b in rep["blind_spots"][:4]:
                    st.markdown(f"- {b['text']}")

    with st.expander("Copy-ready review comment (Markdown)"):
        comment = report.diff_comment(res)
        st.code(comment, language="markdown")
        st.download_button("Download as .md", comment, file_name="change_review.md", key="dl_diff")

    trace_panel(state.get("trace", []), key="diff")
