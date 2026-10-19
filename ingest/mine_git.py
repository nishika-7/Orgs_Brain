"""Load git history into the graph: who changed what, and who owns the code today.

Run AFTER run_ingest (it needs the File and Function nodes to exist):
    uv run python -m ingest.mine_git C:\\Users\\lenovo\\demo_repos\\flask
    uv run python -m ingest.mine_git                      # uses DEMO_REPO_PATH
    uv run python -m ingest.mine_git --max-commits 500 --no-blame

Uses the plain `git` command (must be on your PATH), so no extra Python package is needed.

Adds:
  (Author)-[:AUTHORED]->(Commit)-[:MODIFIED]->(File)          history: who touched which file, when
  (Author)-[:OWNS {lines, share}]->(File | Function)          blame: who wrote the code that exists NOW
  Function.last_modified                                      epoch seconds of the newest line in the function

Two kinds of "who to ask" are stored on purpose:
  history (MODIFIED)  = who has been working here (good for "who is active on this?")
  ownership (OWNS)    = who wrote the code that is still there (good for "who understands this?")
"""
import argparse
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import config
from graph.client import get_graph
from ingest.load_graph import _write


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


# ------------------------------------------------------------------ history

# Files that change in lots of commits for reasons unrelated to the code around them
NOISE_NAMES = {
    "package-lock.json", "yarn.lock", "poetry.lock", "uv.lock", "pipfile.lock", ".gitignore",
    "license", "licence", "requirements.txt",
}
NOISE_SUFFIXES = (
    ".lock", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".woff", ".woff2",
    ".ttf", ".map", ".pyc", ".min.js", ".min.css", ".pkl", ".joblib", ".h5", ".npy",
)


def _is_noise(path: str) -> bool:
    low = path.lower()
    base = low.rsplit("/", 1)[-1]
    return base in NOISE_NAMES or low.endswith(NOISE_SUFFIXES) or "changelog" in base


def artifact_kind(path: str) -> str:
    """Rough category for a non-Python file touched by a commit."""
    low = path.lower()
    if low.endswith((".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".env", ".conf")):
        return "config"
    if low.endswith((".md", ".rst", ".txt")):
        return "docs"
    if low.endswith((".sql",)):
        return "sql"
    if low.endswith((".html", ".css", ".js", ".ts", ".jsx", ".tsx", ".jinja", ".j2")):
        return "web"
    if low.endswith((".csv", ".parquet", ".xlsx", ".ipynb")):
        return "data"
    return "other"


def read_history(repo: Path, max_commits: int) -> list[dict]:
    """Most recent commits (newest first), each with its changed .py files."""
    fmt = "--format=%x1e%H%x1f%an%x1f%ae%x1f%at%x1f%s"
    raw = _git(repo, "log", "--no-merges", f"-n{max_commits}", "--name-only", "--relative", fmt)
    commits = []
    for block in raw.split("\x1e"):
        if not block.strip():
            continue
        lines = block.split("\n")
        parts = lines[0].split("\x1f", 4)
        if len(parts) < 5:
            continue
        sha, name, email, ts, subject = parts
        files = [ln.strip() for ln in lines[1:] if ln.strip() and not _is_noise(ln.strip())]
        commits.append(
            {
                "hash": sha,
                "name": name,
                "email": email.strip().lower(),
                "ts": int(ts),
                "msg": subject[:200],
                "files": files,
                "n_files": len(files),
            }
        )
    return commits


def load_history(commits: list[dict]) -> dict:
    g = get_graph()
    for stmt in (
        "CREATE INDEX FOR (a:Author) ON (a.email)",
        "CREATE INDEX FOR (c:Commit) ON (c.hash)",
        "CREATE INDEX FOR (x:Artifact) ON (x.path)",
    ):
        try:
            g.query(stmt)
        except Exception:
            pass  # already exists

    authors = {}
    for c in commits:  # newest first, so the first name we see is the most recent one
        authors.setdefault(c["email"], c["name"])
    _write(
        "UNWIND $rows AS r MERGE (a:Author {email: r.email}) SET a.name = r.name",
        [{"email": e, "name": n} for e, n in authors.items()],
    )
    _write(
        "UNWIND $rows AS r MERGE (c:Commit {hash: r.hash}) SET c.msg = r.msg, c.ts = r.ts, c.n_files = r.n",
        [{"hash": c["hash"], "msg": c["msg"], "ts": c["ts"], "n": c["n_files"]} for c in commits],
    )
    _write(
        """UNWIND $rows AS r
           MATCH (a:Author {email: r.email}), (c:Commit {hash: r.hash})
           MERGE (a)-[:AUTHORED]->(c)""",
        [{"email": c["email"], "hash": c["hash"]} for c in commits],
    )
    touched = [{"hash": c["hash"], "path": f} for c in commits for f in c["files"] if f.endswith(".py")]
    _write(
        """UNWIND $rows AS r
           MATCH (c:Commit {hash: r.hash}), (f:File {path: r.path})
           MERGE (c)-[:MODIFIED]->(f)""",
        touched,
    )

    # Non-Python files (configs, docs, templates...) become Artifact nodes. They matter for
    # "hidden coupling": things that keep changing together with the code but are not linked to it.
    other = [
        {"hash": c["hash"], "path": f, "kind": artifact_kind(f)}
        for c in commits
        for f in c["files"]
        if not f.endswith(".py")
    ]
    _write(
        """UNWIND $rows AS r
           MATCH (c:Commit {hash: r.hash})
           MERGE (x:Artifact {path: r.path})
           SET x.kind = r.kind
           MERGE (c)-[:MODIFIED]->(x)""",
        other,
    )
    return {
        "authors": len(authors),
        "commits": len(commits),
        "py_file_touches": len(touched),
        "other_file_touches": len(other),
    }


# ------------------------------------------------------------------ blame

def blame_lines(repo: Path, rel: str) -> dict[int, tuple[str, str, int]]:
    """{line number: (email, name, author-time)} for the current version of a file."""
    raw = _git(repo, "blame", "--line-porcelain", "-w", "--", rel)
    out, cur = {}, None
    for line in raw.split("\n"):
        if line.startswith("\t"):  # the source line itself ends one record
            if cur is not None:
                out[cur["final"]] = (
                    cur.get("author-mail", "").strip("<>").lower(),
                    cur.get("author", ""),
                    int(cur.get("author-time", 0) or 0),
                )
            cur = None
        elif cur is None:
            parts = line.split()
            if len(parts) >= 3 and parts[2].isdigit():
                cur = {"final": int(parts[2])}
        else:
            key, _, val = line.partition(" ")
            cur[key] = val
    return out


def load_ownership(repo: Path, max_files: int) -> dict:
    g = get_graph()
    files = [
        r[0]
        for r in g.query("MATCH (f:File) WHERE f.is_test = false RETURN f.path ORDER BY f.loc DESC").result_set
    ][:max_files]

    file_rows, fn_rows, fn_times = [], [], []
    done = failed = 0
    for rel in files:
        try:
            lines = blame_lines(repo, rel)
        except RuntimeError:
            failed += 1  # untracked or deleted file
            continue
        if not lines:
            continue
        done += 1

        per_file = Counter(email for email, _, _ in lines.values())
        names = {email: name for email, name, _ in lines.values()}
        total = sum(per_file.values())
        for email, n in per_file.most_common(5):
            file_rows.append(
                {"email": email, "name": names[email], "path": rel, "lines": n, "share": round(n / total, 3)}
            )

        funcs = g.query(
            "MATCH (fn:Function {file: $f}) RETURN fn.uid, fn.lineno, fn.end_lineno", {"f": rel}
        ).result_set
        for uid, start, end in funcs:
            span = [lines[n] for n in range(start, end + 1) if n in lines]
            if not span:
                continue
            counts = Counter(email for email, _, _ in span)
            fn_times.append({"uid": uid, "ts": max(t for _, _, t in span)})
            for email, n in counts.most_common(5):
                fn_rows.append(
                    {
                        "email": email,
                        "name": next(nm for em, nm, _ in span if em == email),
                        "uid": uid,
                        "lines": n,
                        "share": round(n / len(span), 3),
                    }
                )

    _write(
        """UNWIND $rows AS r
           MERGE (a:Author {email: r.email})
           SET a.name = coalesce(a.name, r.name)
           WITH a, r
           MATCH (f:File {path: r.path})
           MERGE (a)-[o:OWNS]->(f)
           SET o.lines = r.lines, o.share = r.share""",
        file_rows,
    )
    _write(
        """UNWIND $rows AS r
           MERGE (a:Author {email: r.email})
           SET a.name = coalesce(a.name, r.name)
           WITH a, r
           MATCH (fn:Function {uid: r.uid})
           MERGE (a)-[o:OWNS]->(fn)
           SET o.lines = r.lines, o.share = r.share""",
        fn_rows,
    )
    _write("UNWIND $rows AS r MATCH (fn:Function {uid: r.uid}) SET fn.last_modified = r.ts", fn_times)
    return {"files_blamed": done, "files_skipped": failed, "function_owner_edges": len(fn_rows)}


# ------------------------------------------------------------------ CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="Load git history and code ownership into FalkorDB.")
    ap.add_argument("repo", nargs="?", default=None, help="path to the repo (default: DEMO_REPO_PATH)")
    ap.add_argument("--max-commits", type=int, default=800, help="how many recent commits to read")
    ap.add_argument("--max-blame-files", type=int, default=400)
    ap.add_argument("--no-blame", action="store_true", help="skip ownership (faster)")
    args = ap.parse_args()

    repo = Path(args.repo) if args.repo else config.DEMO_REPO_PATH
    if repo is None or not repo.is_dir():
        print("Give a repo path as an argument or set DEMO_REPO_PATH in .env")
        return 1
    repo = repo.resolve()

    try:
        if _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
            print("WARNING: shallow clone, so history is truncated. Re-clone without --depth for real history.")
    except RuntimeError as exc:
        print(f"Not a usable git repository: {exc}")
        return 1

    print(f"Repo: {repo}")
    commits = read_history(repo, args.max_commits)
    print(f"Read {len(commits)} commits")
    print("Loading history:", load_history(commits))

    if not args.no_blame:
        print("Running git blame (ownership)...")
        print("Loaded ownership:", load_ownership(repo, args.max_blame_files))

    g = get_graph()
    print("\nTop owners by lines of code that still exist:")
    res = g.query(
        """MATCH (a:Author)-[o:OWNS]->(f:File)
           RETURN a.name, sum(o.lines) AS lines ORDER BY lines DESC LIMIT 5"""
    )
    for name, lines in res.result_set:
        print(f"  {name}: {lines} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
