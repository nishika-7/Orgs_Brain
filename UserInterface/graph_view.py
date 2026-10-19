"""Draw a blast radius as a Graphviz graph (rendered in the browser by st.graphviz_chart).

No extra dependency and no system Graphviz needed: Streamlit renders the DOT text client-side.
Layout is left-to-right: dependents on the left, the function you would change on the right.
"""
from agent import analysis as A

HOP_COLORS = {1: "#ffb347", 2: "#ffd27f", 3: "#ffe8b8", 4: "#fff3d6"}
MAX_PROD, MAX_TESTS, MAX_INST, MAX_HIDDEN = 14, 4, 3, 4

LEGEND = (
    "**Legend:** red = the function you would change; orange = functions that depend on it "
    "(darker = closer); green = tests that reach it; blue = code that creates its class; "
    "grey dashed = files that keep changing with it but have no code link."
)


def _esc(text) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _short(path: str, parts: int = 2) -> str:
    return "/".join(str(path).split("/")[-parts:])


def _label(qual: str, file: str) -> str:
    return f"{_esc(qual)}\\n{_esc(_short(file))}"


def blast_dot(rep: dict) -> str:
    br = rep["blast"]
    target = br["target"]
    prod = sorted(br["prod_callers"], key=lambda c: (c["hops"], c["file"], c["qualname"]))
    tests = sorted((c for c in br["callers"] if c["is_test"]), key=lambda c: (c["hops"], c["file"]))
    insts = [i for i in br["instantiators"] if not i["is_test"]]
    hidden = rep["hidden"]["hidden"]

    ids: dict[str, str] = {}

    def nid(key: str) -> str:
        return ids.setdefault(key, f"n{len(ids)}")

    out = [
        "digraph G {",
        'rankdir=LR; bgcolor="transparent"; nodesep=0.25; ranksep=0.7;',
        'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10, color="#555555", fillcolor="#ffffff"];',
        'edge [color="#777777", arrowsize=0.7, fontname="Helvetica", fontsize=9];',
    ]
    tid = nid(target["uid"])
    out.append(f'{tid} [label="{_label(target["qualname"], target["file"])}", fillcolor="#ff6b6b", fontcolor="white", penwidth=2];')

    shown = [(c["uid"], c) for c in prod[:MAX_PROD]] + [(c["uid"], c) for c in tests[:MAX_TESTS]]
    for uid, c in shown:
        fill = "#b7e4c7" if c["is_test"] else HOP_COLORS.get(c["hops"], "#fff3d6")
        suffix = "  (test)" if c["is_test"] else ""
        out.append(f'{nid(uid)} [label="{_label(c["qualname"] + suffix, c["file"])}", fillcolor="{fill}"];')

    # real edges between the nodes we are showing
    uids = [target["uid"]] + [u for u, _ in shown]
    edge_pairs = set(A.edges_among(uids))
    has_out = set()
    for a, b in sorted(edge_pairs):
        out.append(f"{nid(a)} -> {nid(b)};")
        has_out.add(a)
    for uid, _ in shown:  # keep the picture connected when an intermediate caller was cut
        if uid not in has_out:
            out.append(f'{nid(uid)} -> {tid} [style=dashed, color="#bbbbbb"];')

    for i in insts[:MAX_INST]:
        key = "inst:" + i["uid"]
        out.append(f'{nid(key)} [label="{_label(i["qualname"], i["file"])}", fillcolor="#cfe8ff"];')
        out.append(f'{nid(key)} -> {tid} [style=dotted, color="#3b82c4", label="creates class"];')

    for h in hidden[:MAX_HIDDEN]:
        key = "hid:" + h["path"]
        out.append(
            f'{nid(key)} [label="{_esc(_short(h["path"]))}\\n[{_esc(h["kind"])}]", shape=note, '
            f'style="dashed,filled", fillcolor="#eeeeee"];'
        )
        out.append(
            f'{nid(key)} -> {tid} [style=dashed, color="#8a63d2", fontcolor="#8a63d2", '
            f'label="changes together {h["together"]}/{rep["hidden"]["total"]}"];'
        )

    more = max(0, len(prod) - MAX_PROD) + max(0, len(tests) - MAX_TESTS)
    if more:
        out.append(f'{nid("more")} [label="+{more} more", shape=plaintext, style=""];')
        out.append(f'{nid("more")} -> {tid} [style=dashed, color="#bbbbbb"];')
    out.append("}")
    return "\n".join(out)
