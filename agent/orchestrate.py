"""Evidence-first answering: code gathers the graph evidence, the LLM only explains it.

Why: letting a model decide which tools to call proved unreliable (it skipped risk_report and
invented verdicts), and every extra tool round trip burns scarce free-tier tokens. Here:

  1. prepare()  finds the function(s) the question is about, picks the analysis that fits the
                question (risk, removal checklist, flow, people, diff), and runs it.
  2. ask()      sends ONE request: the question plus that evidence. If the model is rate limited
                or fails, the raw evidence is returned instead of an error.

The agent keeps its tools for follow-up questions the evidence does not cover.
"""
import re
import time
from dataclasses import dataclass, field

from agent import analysis as A
from agent import tools as T
from graph.client import run_query

STOP = {
    "what", "would", "happen", "happens", "this", "that", "with", "from", "when", "where", "which", "will",
    "are", "was", "been", "any", "all", "stop", "using", "modify", "refactor", "break", "breaks", "risk",
    "risky", "file", "module", "class", "code", "there", "than", "then", "also", "just", "please", "tell",
    "show", "explain", "need", "want", "get", "has", "have", "had", "can", "how", "does", "who", "should",
    "ask", "about", "function", "method", "remove", "delete", "rename", "change", "safe", "the", "and",
    "for", "not", "use", "its", "our", "your", "into", "over", "does", "did", "done", "make", "made",
    "being", "were", "them", "they", "you", "yes", "run", "work", "works", "other", "some", "more", "most",
}

DIFF_WORDS = ("diff", "patch", "pull request", " pr ", "this change", "my change", "these changes", "changes i")
WHO_WORDS = ("who ", "who's", "owner", "owns", "wrote", "maintain", "expert", "reviewer", "ask ")
ONBOARD_WORDS = (
    "where do i start", "where should i start", "start reading", "onboard", "new here", "read first",
    "overview", "core modules", "key files", "getting started", "get started", "new joinee", "new to",
)
FLOW_WORDS = ("what happens when", "how does", "walk me through", "trace", "flow of", "what does it call", "execution")
CHECKLIST_WORDS = (
    "rename", "remove", "delete", "deprecat", "drop ", "replace", "stop using", "not use", "no longer use",
    "where is it used", "where is used", "call sites", "usages", "where do i update", "what do i need to update",
)


@dataclass
class Prepared:
    prompt: str | None = None       # question + evidence, ready for the model
    evidence: str | None = None     # the raw evidence text
    direct: str | None = None       # a reply that needs no model (e.g. "which one do you mean?")
    choices: list[dict] = field(default_factory=list)
    used: list[str] = field(default_factory=list)


def _tokens(question: str) -> list[str]:
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", question)
    out = []
    for tok in raw:
        parts = [tok] + (tok.split(".") if "." in tok else [])
        for p in parts:
            if len(p) >= 3 and p.lower() not in STOP and p not in out:
                out.append(p)
    return out


def _file_hints(question: str, tokens: list[str]) -> set[str]:
    """Words in the question that name a file (models, optimizer.py, src/pkg/models.py)."""
    hints = {m.replace("\\", "/") for m in re.findall(r"[\w./\\-]+\.py", question)}
    stems = [t + ".py" for t in tokens]
    if stems:
        for (path,) in run_query("orchestrate", "MATCH (f:File) WHERE f.name IN $n RETURN f.path", {"n": stems}).result_set:
            hints.add(path)
    return hints


def _candidates(tokens: list[str]) -> dict[str, list[dict]]:
    if not tokens:
        return {}
    rows = run_query(
        "orchestrate",
        """MATCH (fn:Function) WHERE fn.name IN $t OR fn.qualname IN $t
           RETURN fn.uid, fn.qualname, fn.name, fn.file, fn.lineno, fn.is_test""",
        {"t": tokens},
    ).result_set
    by_token: dict[str, list[dict]] = {}
    for uid, qual, name, file, lineno, is_test in rows:
        cand = {"uid": uid, "qualname": qual, "file": file, "lineno": lineno, "is_test": bool(is_test)}
        for tok in {qual, name} & set(tokens):
            by_token.setdefault(tok, []).append(cand)
    return by_token


def _narrow(cands: list[dict], hints: set[str]) -> list[dict]:
    if len(cands) > 1 and hints:
        keep = [c for c in cands if any(c["file"] == h or c["file"].endswith("/" + h) or c["file"].endswith(h) for h in hints)]
        cands = keep or cands
    if len(cands) > 1:
        non_test = [c for c in cands if not c["is_test"]]
        cands = non_test or cands
    return cands


def prepare(question: str, diff_text: str | None = None) -> Prepared:
    q = " " + question.lower() + " "
    tokens = _tokens(question)
    hints = _file_hints(question, tokens)
    prep = Prepared()

    # 1) a diff passed in (web app) or loaded globally (terminal chat)
    if (diff_text or T.has_current_diff()) and any(w in q for w in DIFF_WORDS):
        prep.evidence = T.analyze_diff(diff_text or "CURRENT")
        prep.used = ["analyze_diff"]
        return _wrap(question, prep)

    # 2) find the function(s) the question is about
    by_token = _candidates(tokens)
    chosen: list[dict] = []
    skip: set[str] = set()  # parts of a qualified name that already resolved (e.g. "predict" in "A.predict")
    for tok, cands in by_token.items():
        if "." in tok and len(_narrow(cands, hints)) == 1:
            skip.update(tok.split("."))
    for tok, cands in by_token.items():
        if tok in skip and "." not in tok:
            continue
        cands = _narrow(cands, hints)
        if len(cands) == 1:
            if cands[0]["uid"] not in [c["uid"] for c in chosen]:
                chosen.append(cands[0])
        elif len(cands) > 1:
            prep.choices = cands[:6]
            lines = [f"I found {len(cands)} functions matching `{tok}`. Which one do you mean?"]
            lines += [f"- `{c['qualname']}` in {c['file']}:{c['lineno']}" for c in cands[:6]]
            lines.append("Reply with the qualified name (for example the first one above) or mention the file.")
            prep.direct = "\n".join(lines)
            return prep
    chosen = chosen[:2]

    # 3) no function: onboarding or file-level people questions
    if not chosen:
        if any(w in q for w in ONBOARD_WORDS):
            prep.evidence = T.key_files(8)
            prep.used = ["key_files"]
            return _wrap(question, prep)
        if hints and any(w in q for w in WHO_WORDS):
            path = sorted(hints)[0]
            prep.evidence = T.who_to_ask(path)
            prep.used = ["who_to_ask"]
            return _wrap(question, prep)
        return prep  # nothing to precompute: the model can use its tools

    # 4) pick the analysis that fits the question
    parts, used = [], []
    wants_checklist = any(w in q for w in CHECKLIST_WORDS)
    wants_who = any(w in q for w in WHO_WORDS)
    wants_flow = any(w in q for w in FLOW_WORDS)
    for c in chosen:
        if wants_who and not wants_checklist:
            parts.append(T.who_to_ask(c["uid"]))
            used.append("who_to_ask")
        elif wants_flow and not wants_checklist:
            parts.append(T.trace_flow(c["uid"]))
            used.append("trace_flow")
        else:
            parts.append(T.risk_report(c["uid"]))
            used.append("risk_report")
            if wants_checklist:
                parts.append(T.update_checklist(c["uid"]))
                used.append("update_checklist")
    prep.evidence = "\n\n".join(parts)
    prep.used = used
    return _wrap(question, prep)


def _wrap(question: str, prep: Prepared) -> Prepared:
    prep.prompt = (
        f"{question}\n\n"
        "--- GRAPH EVIDENCE (computed from the repository graph by code; authoritative) ---\n"
        f"{prep.evidence}\n"
        "--- END EVIDENCE ---\n"
        "Answer using only this evidence."
    )
    return prep


def _compact_history(agent, question: str) -> None:
    """Swap the big evidence prompt in the history back to the bare question.

    The agent re-sends its whole history on every turn; keeping every old evidence block would
    quickly exhaust a free-tier tokens-per-minute budget. The model's own answer stays in the
    history, so follow-ups like "and who should review it?" still make sense.
    """
    try:
        for msg in reversed(agent.messages):
            if msg.get("role") == "user":
                blocks = msg.get("content") or []
                if blocks and isinstance(blocks[0], dict) and "GRAPH EVIDENCE" in blocks[0].get("text", ""):
                    blocks[0]["text"] = question
                break
    except Exception:  # noqa: BLE001  (history compaction must never break an answer)
        pass


def _rate_limited(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(w in text for w in ("rate", "429", "throttl", "quota", "too many", "tokens per"))


def ask(agent, question: str, diff_text: str | None = None) -> tuple[str, Prepared]:
    """Answer one question. Returns (answer text, the Prepared object for the UI)."""
    prep = prepare(question, diff_text)
    if prep.direct:
        return prep.direct, prep

    prompt = prep.prompt or question
    last_exc = None
    for attempt in range(3):
        try:
            answer = str(agent(prompt))
            if prep.prompt:
                _compact_history(agent, question)
            return answer, prep
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not _rate_limited(exc):
                break
            time.sleep(8 * (attempt + 1))

    if prep.evidence:  # degrade gracefully: the analysis itself does not need the model
        return (
            "The language model is unavailable right now (" + type(last_exc).__name__ + "), "
            "so here is the raw analysis from the graph:\n\n" + prep.evidence
        ), prep
    raise last_exc
