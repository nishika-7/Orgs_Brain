"""Turn raw parse output into graph edges. Pure Python, no database code.

Resolution rules are deliberately simple and honest about being approximate:

Imports   `import a.b` / `from a import b` are matched to files inside the repo.
          Anything not found locally (and not in the standard library) becomes
          an external Package.

Calls     A call to `foo` is linked to a function named `foo`, preferring
          (1) the same file, then (2) files this file imports, then (3) the
          single global match if exactly one exists. Ambiguous names are
          skipped on purpose.

          How the call is WRITTEN matters, because obj.method() hides the type of obj:
            foo()          plain name: all three steps; step 3 is skipped for builtin
                           names (len, type, print...) so they never link to user code
            self.foo()     method of this class (or a base): all three steps
            obj.foo()      unknown receiver: all three steps, but names that exist on
                           builtin str/list/dict/file objects (lower, get, append...)
                           are skipped entirely, otherwise every `s.lower()` in the
                           repo would count as a call to a user-defined `lower`
"""
import builtins
import sys
from collections import defaultdict
from pathlib import PurePosixPath

import config

# Method names that builtin containers/strings/files already have. `obj.get()` on an
# unknown receiver is far more likely a dict than a user-defined `get`.
COMMON_METHOD_NAMES = {
    "append", "extend", "insert", "remove", "pop", "clear", "copy", "index", "count", "sort",
    "reverse", "keys", "values", "items", "get", "update", "setdefault", "add", "discard",
    "join", "split", "rsplit", "strip", "lstrip", "rstrip", "lower", "upper", "title",
    "capitalize", "replace", "find", "rfind", "startswith", "endswith", "format", "encode",
    "decode", "read", "write", "readline", "readlines", "close", "flush", "seek", "tell",
}
BUILTIN_NAMES = set(dir(builtins))


def module_name_for(rel: str) -> str:
    """Plain path-based name: 'pkg/sub/mod.py' -> 'pkg.sub.mod' ; 'pkg/__init__.py' -> 'pkg'."""
    parts = rel[:-3].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class ImportResolver:
    """Maps import names to files inside the repo.

    Python names a module starting from the first folder that is NOT a package
    (a package = a folder with __init__.py). So backend/app/models.py is
    imported as `app.models` when backend/ has no __init__.py. Each file is
    registered under several aliases so common layouts all resolve:
      1. its package-chain name (the real Python name)
      2. its full path name (e.g. 'backend.app.models')
      3. the full path name without a leading 'src.'
    """

    def __init__(self, rel_paths: list[str], root_name: str | None = None):
        self.root_name = root_name
        self.pkg_dirs = {
            PurePosixPath(r).parent.as_posix() for r in rel_paths if r.endswith("__init__.py")
        }
        self.module_map: dict[str, str] = {}
        self.name_of: dict[str, str] = {}
        for rel in rel_paths:
            chain = self._chain_name(rel)
            self.name_of[rel] = chain or module_name_for(rel)
            aliases = [chain, module_name_for(rel)]
            full = module_name_for(rel)
            if full.startswith("src."):
                aliases.append(full[4:])
            for alias in aliases:
                if not alias:
                    continue
                # a lone top-level file named like a stdlib module (types.py, queue.py)
                # would otherwise hijack `import types`
                if "." not in alias and alias in sys.stdlib_module_names and alias != self.root_name:
                    continue
                self.module_map.setdefault(alias, rel)
        self.local_tops = {name.split(".")[0] for name in self.module_map}

    def _chain_name(self, rel: str) -> str:
        p = PurePosixPath(rel)
        parts = [] if p.stem == "__init__" else [p.stem]
        d = p.parent
        while d.as_posix() in self.pkg_dirs:
            if d.as_posix() == ".":  # the analyzed folder is itself a package
                if self.root_name:
                    parts.insert(0, self.root_name)
                break
            parts.insert(0, d.name)
            d = d.parent
        return ".".join(parts)

    def resolve(self, imp: dict, current_rel: str) -> list[tuple[str, str, list[str]]]:
        """Return [("file", rel_path, imported_names)] or [("package", name, [])] or []."""
        module, level = imp["module"], imp["level"]

        if level:  # relative import: build the absolute module name first
            cur = self.name_of.get(current_rel, "")
            parts = cur.split(".") if cur else []
            if not current_rel.endswith("__init__.py"):
                parts = parts[:-1]  # a plain module lives inside its parent package
            if level > 1:
                parts = parts[: max(0, len(parts) - (level - 1))]
            base = ".".join(parts)
            module = f"{base}.{module}" if base and module else (base or module)

        def lookup(candidates):
            found = []
            for cand in candidates:
                target = self.module_map.get(cand)
                if target and target != current_rel and target not in found:
                    found.append(target)
            return found

        # 'from pkg import submodule' -> link the submodule file(s) only.
        # If none match, the names are things defined inside pkg itself, so link pkg.
        submodules = lookup([f"{module}.{n}" if module else n for n in imp["names"]])
        if submodules:
            return [("file", f, []) for f in submodules]
        files = lookup([module])
        if files:
            return [("file", f, list(imp["names"])) for f in files]

        top = module.split(".")[0] if module else ""
        if level == 0 and top and top not in self.local_tops and top not in sys.stdlib_module_names:
            return [("package", top, [])]
        return []


def resolve_imports(parsed: list[dict], resolver: ImportResolver):
    """Return (file_edges, package_edges, imported_files_by_path).

    file_edges maps (importer, imported) -> {"names": set of imported names, "lines": set of lines}
    so a removal checklist can point at the exact `from x import y` line.
    """
    file_edges: dict[tuple[str, str], dict] = {}
    package_edges = set()
    imported: dict[str, set[str]] = defaultdict(set)
    for pf in parsed:
        for imp in pf["imports"]:
            for kind, target, names in resolver.resolve(imp, pf["path"]):
                if kind == "file":
                    edge = file_edges.setdefault((pf["path"], target), {"names": set(), "lines": set()})
                    edge["names"].update(names)
                    edge["lines"].add(imp["lineno"])
                    imported[pf["path"]].add(target)
                else:
                    package_edges.add((pf["path"], target))
    return file_edges, package_edges, imported


def function_uid(path: str, qualname: str) -> str:
    return f"{path}::{qualname}"


def _pick_targets(name, own_path, by_name, imported_files, kind="name"):
    """Apply the same-file -> imported-file -> unique-global preference, kind-aware."""
    if kind == "attr" and name in COMMON_METHOD_NAMES:
        return []  # almost certainly a builtin method, not ours
    cands = by_name.get(name, [])
    if not cands:
        return []
    same = [uid for uid, path in cands if path == own_path]
    if same:
        targets = same
    else:
        via_import = [uid for uid, path in cands if path in imported_files]
        if via_import:
            targets = via_import
        elif len(cands) == 1 and kind != "ref" and not (kind == "name" and name in BUILTIN_NAMES):
            targets = [cands[0][0]]
        else:
            return []
    return targets if len(targets) <= config.MAX_CALL_TARGETS else []


def resolve_calls(parsed: list[dict], imported: dict[str, set[str]]):
    """Return (calls, refs): each maps (caller_uid, target_uid) -> [line numbers].

    refs are functions passed around as values (callbacks, thread targets, sort keys). They are
    real dependencies, so a change can break them, but they are not calls.
    """
    by_name = defaultdict(list)
    for pf in parsed:
        for fn in pf["functions"]:
            by_name[fn["name"]].append((function_uid(pf["path"], fn["qualname"]), pf["path"]))

    calls: dict[tuple[str, str], list[int]] = defaultdict(list)
    refs: dict[tuple[str, str], list[int]] = defaultdict(list)
    for pf in parsed:
        own_imports = imported.get(pf["path"], set())
        for caller_q, callee_name, kind, line in pf["calls"]:
            caller = function_uid(pf["path"], caller_q)
            for target in _pick_targets(callee_name, pf["path"], by_name, own_imports, kind):
                if target != caller:  # ignore recursion to keep the graph readable
                    (refs if kind == "ref" else calls)[(caller, target)].append(line)
    return dict(calls), dict(refs)


def resolve_inheritance(parsed: list[dict], imported: dict[str, set[str]]) -> set[tuple[str, str]]:
    by_name = defaultdict(list)
    for pf in parsed:
        for cls in pf["classes"]:
            by_name[cls["name"]].append((function_uid(pf["path"], cls["qualname"]), pf["path"]))

    edges = set()
    for pf in parsed:
        for cls in pf["classes"]:
            child = function_uid(pf["path"], cls["qualname"])
            for base in cls["bases"]:
                for target in _pick_targets(base, pf["path"], by_name, imported.get(pf["path"], set())):
                    if target != child:
                        edges.add((child, target))
    return edges


def resolve_instantiations(parsed: list[dict], imported: dict[str, set[str]]) -> dict[tuple[str, str], list[int]]:
    """(function)-[:INSTANTIATES]->(class): a call like User(...) where User is a class in the repo.

    Without these edges, changing a class would look like it affects nobody,
    because instantiation is a call to the CLASS, not to a function.
    """
    by_name = defaultdict(list)
    for pf in parsed:
        for cls in pf["classes"]:
            by_name[cls["name"]].append((function_uid(pf["path"], cls["qualname"]), pf["path"]))

    edges: dict[tuple[str, str], list[int]] = defaultdict(list)
    for pf in parsed:
        own_imports = imported.get(pf["path"], set())
        for caller_q, callee_name, kind, line in pf["calls"]:
            if kind == "ref" or callee_name not in by_name:
                continue
            caller = function_uid(pf["path"], caller_q)
            for target in _pick_targets(callee_name, pf["path"], by_name, own_imports, kind):
                edges[(caller, target)].append(line)
    return dict(edges)


def directory_structure(file_paths: list[str]):
    """Return (dir_paths, dir_to_dir_edges, dir_to_file_edges). Root directory is '.'."""
    dirs = {"."}
    dir_edges, file_edges = set(), set()
    for rel in file_paths:
        parent = PurePosixPath(rel).parent.as_posix()
        file_edges.add((parent, rel))
        child = parent
        while child != ".":
            dirs.add(child)
            up = PurePosixPath(child).parent.as_posix()
            dir_edges.add((up, child))
            child = up
    return dirs, dir_edges, file_edges
