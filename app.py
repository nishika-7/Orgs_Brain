"""Safe-Change Guide: web UI.

    uv run streamlit run app.py

Tabs:
  Ask                  chat; the graph evidence is computed by code, the model only explains it
  Review a change      paste a diff or a GitHub PR link -> risk, missing files, tests, reviewers
  Explore a function   risk card with a blast-radius graph for any function
  How it works         data model and the Cypher behind the answers

Only the chat explanation needs the language model. Everything else runs on the graph alone.
"""
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Safe-Change Guide", page_icon="🛡️", layout="wide")

# On Streamlit Cloud, secrets live in st.secrets. Copy them into the environment BEFORE importing
# config, so the rest of the code (which reads environment variables) works unchanged.
try:
    for _key, _val in st.secrets.items():
        if isinstance(_val, (str, int, float, bool)):
            os.environ.setdefault(_key, str(_val))
except Exception:  # no secrets file locally: that is fine, .env is used instead
    pass

import config  # noqa: E402
from agent import analysis as A  # noqa: E402
from agent.orchestrate import ask, prepare  # noqa: E402
from graph import client  # noqa: E402
from UserInterface import components as C  # noqa: E402
from UserInterface   import graph_view  # noqa: E402


# ------------------------------------------------------------------ setup

@st.cache_resource(show_spinner=False)
def ensure_repo():
    """Source lines and text search need the repo files. If it is not local, clone it (shallow)."""
    root = config.DEMO_REPO_PATH
    if root and Path(root).is_dir():
        return str(root)
    url = os.getenv("DEMO_REPO_URL", "").strip()
    if not url:
        return None
    dest = Path(tempfile.gettempdir()) / "demo_repo"
    if not dest.exists():
        try:
            subprocess.run(["git", "clone", "--depth", "1", url, str(dest)], check=True, capture_output=True, timeout=180)
        except Exception:  # noqa: BLE001
            return None
    config.DEMO_REPO_PATH = dest
    return str(dest)


@st.cache_data(ttl=300, show_spinner=False)
def cached_stats():
    return A.graph_stats()


@st.cache_data(ttl=300, show_spinner=False)
def cached_hot_spots():
    return A.hot_spots(6)


def check_graph():
    try:
        nodes, rels = cached_stats()
    except Exception as exc:  # noqa: BLE001
        st.error("Cannot reach the graph database. Check the FALKORDB_* settings (host, port, username, password).")
        st.code(f"{type(exc).__name__}: {exc}")
        st.stop()
    if not nodes.get("Function"):
        st.warning(
            "The graph is empty. Build it first:\n\n"
            "`uv run python -m ingest.run_ingest <repo> --reset` then `uv run python -m ingest.mine_git <repo>`"
        )
        st.stop()
    return nodes, rels


def get_agent():
    if "agent_ready" not in st.session_state:
        try:
            from agent.agent import build_agent

            st.session_state.agent = build_agent()
            st.session_state.agent_error = None
        except Exception as exc:  # noqa: BLE001  (usually: no API key configured)
            st.session_state.agent = None
            st.session_state.agent_error = str(exc)
        st.session_state.agent_ready = True
    return st.session_state.agent


# ------------------------------------------------------------------ analyses

def run_function_analysis(uid: str, depth: int) -> dict:
    client.clear_trace()
    rep = A.risk_report(uid, depth)
    dot = graph_view.blast_dot(rep)
    return {"rep": rep, "dot": dot, "trace": client.snapshot_trace(), "checklist": None, "uid": uid}


def run_diff_analysis(text: str) -> dict:
    client.clear_trace()
    res = A.analyze_diff(text)
    dots = [graph_view.blast_dot(r) for r in res.get("reports", [])]
    return {"res": res, "dots": dots, "trace": client.snapshot_trace(), "text": text}


PR_RE = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")


def fetch_pr_diff(url: str) -> str:
    m = PR_RE.match(url.strip())
    if not m:
        raise ValueError("Paste a link like https://github.com/owner/repo/pull/123")
    owner, repo, num = m.groups()
    req = urllib.request.Request(
        f"https://github.com/{owner}/{repo}/pull/{num}.diff", headers={"User-Agent": "safe-change-guide"}
    )
    last = None
    for _ in range(2):  # one retry: GitHub or the network occasionally hiccups
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310  (fixed https host)
                return resp.read(2_000_000).decode("utf-8", errors="replace")
        except urllib.error.HTTPError:
            raise  # 404 and friends will not get better by retrying
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last


# ------------------------------------------------------------------ chat

def handle_question(q: str, agent) -> None:
    st.session_state.messages.append({"role": "user", "content": q})
    with st.chat_message("user"):
        st.markdown(q)
    diff_text = (st.session_state.get("diff_state") or {}).get("text")
    with st.chat_message("assistant"):
        with st.spinner("Reading the graph..."):
            client.clear_trace()
            prep = None
            try:
                if agent is not None:
                    answer, prep = ask(agent, q, diff_text)
                else:  # no language model: show the computed evidence itself
                    prep = prepare(q, diff_text)
                    answer = prep.direct or prep.evidence or (
                        "I could not find a function in your question. Name one (for example `save_user`), "
                        "or add an API key so the language model can help with open-ended questions."
                    )
            except Exception as exc:  # noqa: BLE001
                answer = f"Something went wrong: `{type(exc).__name__}: {exc}`"
            trace = client.snapshot_trace()
    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": answer,
            "question": q,
            "used": prep.used if prep else [],
            "evidence": prep.evidence if prep else None,
            "choices": prep.choices if prep else [],
            "trace": trace,
        }
    )
    st.rerun()


def render_messages() -> None:
    msgs = st.session_state.messages
    for i, m in enumerate(msgs):
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m["role"] != "assistant":
                continue
            if m.get("choices") and i == len(msgs) - 1:
                for j, c in enumerate(m["choices"]):
                    if st.button(f"{c['qualname']}  ·  {c['file']}:{c['lineno']}", key=f"choice_{i}_{j}"):
                        st.session_state.pending = f"{m['question']} (I mean `{c['qualname']}` in {c['file']})"
                        st.rerun()
            if m.get("used") or m.get("trace"):
                C.trace_panel(m.get("trace", []), m.get("evidence"), m.get("used"), key=f"m{i}")


# ------------------------------------------------------------------ page

nodes, rels = check_graph()
ensure_repo()
agent = get_agent()
st.session_state.setdefault("messages", [])

st.title("🛡️ Safe-Change Guide")
st.caption(
    "What is safe to change, and who should I ask? Answers come from a graph of the code **and** its git history, "
    "so they include things plain code search cannot see."
)

with st.sidebar:
    st.header("The graph")
    c1, c2 = st.columns(2)
    c1.metric("Files", nodes.get("File", 0))
    c2.metric("Functions", nodes.get("Function", 0))
    c1.metric("Authors", nodes.get("Author", 0))
    c2.metric("Commits", nodes.get("Commit", 0))
    st.caption(
        f"{rels.get('CALLS', 0)} call edges, {rels.get('REFERENCES', 0)} callback links, "
        f"{rels.get('IMPORTS', 0)} imports, {rels.get('OWNS', 0)} ownership links"
    )
    st.divider()
    if agent is None:
        st.info("Chat runs in **evidence-only mode** (no language model configured). Everything else works as normal.")
        if st.session_state.get("agent_error"):
            with st.expander("Why?"):
                st.code(st.session_state.agent_error)
    else:
        st.success(f"Language model: `{os.getenv('MODEL_ID', 'openai/gpt-oss-120b')}`")
    st.caption(
        "Static analysis only: it lists the dependents it can see and never claims a change is safe. "
        "Each answer shows exactly which graph queries produced it."
    )

tab_ask, tab_diff, tab_fn, tab_how = st.tabs(["💬 Ask", "🔍 Review a change", "🎯 Explore a function", "⚙️ How it works"])

# ---------------------------------------------------------------- Ask
with tab_ask:
    hot = cached_hot_spots()
    if not st.session_state.messages and hot:
        st.markdown("**Try one of these:**")
        examples = [f"What would happen if I remove {hot[0]['qualname']}?"]
        if len(hot) > 1:
            examples.append(f"Is it safe to change {hot[1]['qualname']}?")
        examples.append(f"Who should I ask about {hot[min(2, len(hot) - 1)]['file']}?")
        examples.append("Where should I start reading this repo?")
        cols = st.columns(len(examples))
        for k, text in enumerate(examples):
            if cols[k].button(text, key=f"ex_{k}"):
                st.session_state.pending = text
                st.rerun()

    render_messages()
    typed = st.chat_input("Ask: what if I remove X? who should review Y? where do I start?")
    question = st.session_state.pop("pending", None) or typed
    if question:
        handle_question(question, agent)

# ---------------------------------------------------------------- Review a change
with tab_diff:
    st.markdown(
        "Paste a code change and get the risk, the files that usually change with it but are **missing** from it, "
        "the tests to run, and who should review it. No language model needed."
    )
    mode = st.radio("Where is the change?", ["Paste a diff", "Upload a .diff file", "GitHub pull request link"], horizontal=True)
    text = None
    if mode == "Paste a diff":
        text = st.text_area("Unified diff (output of `git diff`)", height=200, key="diff_text")
    elif mode == "Upload a .diff file":
        up = st.file_uploader("Diff or patch file", type=["diff", "patch", "txt"])
        if up is not None:
            text = up.getvalue().decode("utf-8", errors="replace")
    else:
        url = st.text_input("Pull request URL", placeholder="https://github.com/owner/repo/pull/123")
        st.caption(
            "Works for public repositories. The graph should match the repository the PR targets, at about the "
            "commit it branches from; for old PRs the line numbers may map to different functions."
        )
        if st.button("Fetch the PR diff") and url:
            try:
                st.session_state.pr_text = fetch_pr_diff(url)
            except Exception as exc:  # noqa: BLE001
                st.session_state.pr_text = None
                st.error(f"Could not fetch the PR: {exc}")
        text = st.session_state.get("pr_text")
        if text:
            st.success(f"Fetched {len(text):,} characters.")

    if st.button("Analyze change", type="primary", disabled=not (text and text.strip())):
        with st.spinner("Mapping the change onto the graph..."):
            st.session_state.diff_state = run_diff_analysis(text)
            st.session_state.pop("diff_explain", None)

    state = st.session_state.get("diff_state")
    if state:
        st.divider()
        C.render_diff_result(state)
        if "error" not in state["res"] and agent is not None:
            if st.button("Explain this change in plain English (uses the language model)"):
                with st.spinner("Writing the explanation..."):
                    answer, _ = ask(agent, "Analyze this change: what could break and who should review it?", state["text"])
                    st.session_state.diff_explain = answer
            if st.session_state.get("diff_explain"):
                st.markdown(st.session_state.diff_explain)

# ---------------------------------------------------------------- Explore a function
with tab_fn:
    st.markdown("Pick any function to see its blast radius, hidden coupling, blind spots, tests and reviewers.")
    depth = st.slider("How many call hops to follow", 1, 4, 3, key="fn_depth")
    query = st.text_input("Search for a function (name or part of it)", key="fn_query")
    if query.strip():
        found = A.search_functions(query, 15)
        if found:
            pick = st.selectbox(
                "Matches", range(len(found)), format_func=lambda i: f"{found[i]['qualname']}  ·  {found[i]['file']}", key="fn_pick"
            )
            if st.button("Analyze risk", type="primary"):
                with st.spinner("Analyzing..."):
                    st.session_state.explore = run_function_analysis(found[pick]["uid"], depth)
        else:
            st.caption("No function matches that text.")
    hot = cached_hot_spots()
    if hot:
        st.caption("Or start from the most depended-on functions:")
        cols = st.columns(min(3, len(hot)))
        for k, h in enumerate(hot[:3]):
            if cols[k].button(f"{h['qualname']} ({h['callers']} callers)", key=f"hot_{k}"):
                with st.spinner("Analyzing..."):
                    st.session_state.explore = run_function_analysis(h["uid"], depth)
    if st.session_state.get("explore"):
        st.divider()
        C.render_function_report(st.session_state.explore, key="explore")

# ---------------------------------------------------------------- How it works
with tab_how:
    st.markdown(
        """
### How it works
1. **Parse** every Python file (no code is executed): functions, classes, imports, who calls whom, with line numbers.
2. **Mine git history**: authors, commits, which files change together, and `git blame` for who wrote the code that exists now.
3. **Store it all in FalkorDB** as a graph, then answer questions by *traversing* it.
4. **Explain**: code gathers the evidence; the language model only turns it into readable text.

### What the graph adds over searching the code
| Question | Why a graph |
|---|---|
| Everything that breaks if I change X | variable-depth traversal over `CALLS` / `REFERENCES` |
| Files that keep changing with X but have no code link | `Commit` nodes connect files that code analysis cannot |
| Who should review | `OWNS` (blame) and `AUTHORED` edges, combined across the dependents |
| What to update when I rename X | edges carry the exact call-site line numbers |

### Data model
`Directory -CONTAINS-> File -DEFINES-> Function | Class` ·
`File -IMPORTS{names,lines}-> File` · `Function -CALLS{sites}-> Function` · `Function -REFERENCES{sites}-> Function` ·
`Function -INSTANTIATES-> Class` · `Class -INHERITS-> Class` ·
`Author -AUTHORED-> Commit -MODIFIED-> File | Artifact` · `Author -OWNS{lines,share}-> File | Function`
"""
    )
    st.markdown("##### The Cypher behind the blast radius")
    st.code(
        "MATCH p = (c:Function)-[:CALLS|REFERENCES*1..3]->(t:Function {uid: $uid})\n"
        "RETURN c.qualname, c.file, c.is_test, min(length(p)) AS hops\n"
        "ORDER BY hops LIMIT 300",
        language="sql",
    )
    st.markdown("##### The Cypher behind hidden coupling")
    st.code(
        "MATCH (f:File {path: $p})<-[:MODIFIED]-(c:Commit)-[:MODIFIED]->(g)\n"
        "WHERE g.path <> $p AND c.n_files <= 25      -- ignore bulk commits\n"
        "RETURN g.path, labels(g)[0], count(DISTINCT c) AS together\n"
        "ORDER BY together DESC",
        language="sql",
    )
    st.markdown("##### Honest limits")
    st.markdown(
        "- It sees what static analysis can see. Dynamic dispatch, reflection, string-based wiring and code outside "
        "the repository are invisible, so it reports *known* dependents and flags specific blind spots; it never says a change is safe.\n"
        "- Hidden coupling is per file (not per function) and only as good as the project's commit habits.\n"
        "- Call links are matched by name with several guards; ambiguous names are skipped."
    )
    st.markdown("##### Graph contents")
    st.dataframe(
        [{"type": k, "count": v} for k, v in sorted(nodes.items(), key=lambda kv: -kv[1])]
        + [{"type": f"-[{k}]->", "count": v} for k, v in sorted(rels.items(), key=lambda kv: -kv[1])],
        hide_index=True,
    )
