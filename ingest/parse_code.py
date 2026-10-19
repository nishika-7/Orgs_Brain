"""Parse Python source files into plain dictionaries. No database code here.

For each file we extract:
  functions  - name, qualified name (Class.method), line range, indent, docstring line,
               decorators, and "blind spot" flags (see below)
  classes    - name, bases, line range
  imports    - raw import statements with line numbers (resolved to files in resolve.py)
  calls      - (caller qualname, callee name, kind, line) where kind is
                 "name"  foo()
                 "self"  self.foo() / cls.foo() / super().foo()
                 "attr"  obj.foo()          (receiver type unknown)
                 "ref"   foo passed as a value: callbacks, thread targets, sort keys...

Blind-spot flags (things static analysis cannot follow), stored per function:
  escapes           defined inside another function AND returned from it (a closure that callers
                    hold in a variable, so the real call sites are invisible)
  returns_callable  returns functions as values
  dynamic           uses getattr/eval/importlib... (may call or look up code by string)

This is static analysis, so call information is approximate: Python is dynamic.
"""
import ast
import os
from collections import defaultdict
from pathlib import Path

import config

DYNAMIC_CALLS = {
    "getattr", "setattr", "delattr", "eval", "exec", "__import__",
    "globals", "locals", "vars", "import_module",
}


def iter_python_files(root: Path):
    """Yield every .py file under root, skipping virtualenvs, caches, etc."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in config.IGNORE_DIRS and not d.endswith(".egg-info")
        ]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _expr_name(node) -> str | None:
    """Simple name of a Name (foo) or Attribute (obj.foo -> foo) expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _doc_line(node) -> str | None:
    doc = ast.get_docstring(node)
    if not doc:
        return None
    return doc.strip().splitlines()[0][:200]


def _start_line(node) -> int:
    """First line of a def/class including its decorators (so a changed decorator maps to it)."""
    return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])


def _decorator_name(node) -> str | None:
    """'@app.route("/x")' -> 'app.route' ; '@login_required' -> 'login_required'."""
    target = node.func if isinstance(node, ast.Call) else node
    try:
        return ast.unparse(target)
    except Exception:
        return None


class _Collector(ast.NodeVisitor):
    def __init__(self):
        self.functions: list[dict] = []
        self.classes: list[dict] = []
        self.imports: list[dict] = []
        self.calls: list[tuple[str, str, str, int]] = []  # (caller, callee, kind, line)
        self._scope: list[str] = []        # names of enclosing classes/functions
        self._func_stack: list[str] = []   # qualnames of enclosing functions
        self._by_qual: dict[str, dict] = {}
        self._nested = defaultdict(set)    # function qualname -> names of defs directly inside it
        self._returned = defaultdict(set)  # function qualname -> names it returns
        self._dynamic = defaultdict(set)   # function qualname -> dynamic features it uses

    def _qual(self, name: str) -> str:
        return ".".join(self._scope + [name])

    # ---- definitions ----
    def visit_ClassDef(self, node):
        bases = [b for b in (_expr_name(x) for x in node.bases) if b]
        self.classes.append(
            {
                "name": node.name,
                "qualname": self._qual(node.name),
                "lineno": _start_line(node),
                "end_lineno": getattr(node, "end_lineno", node.lineno),
                "indent": node.col_offset,
                "bases": bases,
                "doc": _doc_line(node),
            }
        )
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def _visit_function(self, node):
        qual = self._qual(node.name)
        parent = self._func_stack[-1] if self._func_stack else None
        decorators = [d for d in (_decorator_name(x) for x in node.decorator_list) if d]
        info = {
            "name": node.name,
            "qualname": qual,
            "lineno": _start_line(node),
            "end_lineno": getattr(node, "end_lineno", node.lineno),
            "indent": node.col_offset,
            "doc": _doc_line(node),
            # e.g. "app.route,login_required": functions a framework calls for us
            "decorators": ",".join(decorators)[:200],
            "nested_in": parent or "",
            "escapes": False,
            "returns_callable": False,
            "dynamic": "",
        }
        self.functions.append(info)
        self._by_qual[qual] = info
        if parent:
            self._nested[parent].add(node.name)

        # A decorator is a dependency of the function it decorates: change @login_required and
        # every view using it changes behavior. (Bare decorators are not Call nodes, so record
        # them explicitly.) Calls INSIDE the decorator expression belong to the enclosing scope.
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Name):
                self.calls.append((qual, target.id, "name", dec.lineno))
            elif isinstance(target, ast.Attribute):
                self.calls.append((qual, target.attr, "attr", dec.lineno))
            self.visit(dec)

        self._scope.append(node.name)
        self._func_stack.append(qual)
        for stmt in node.body:
            self.visit(stmt)
        self._func_stack.pop()
        self._scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    # ---- what happens inside functions ----
    def visit_Return(self, node):
        if self._func_stack and node.value is not None:
            names = self._returned[self._func_stack[-1]]
            values = node.value.elts if isinstance(node.value, (ast.Tuple, ast.List)) else [node.value]
            for v in values:
                if isinstance(v, ast.Name):
                    names.add(v.id)
                elif isinstance(v, ast.Lambda):
                    names.add("<lambda>")
        self.generic_visit(node)

    def visit_Call(self, node):
        if self._func_stack:
            caller = self._func_stack[-1]
            func = node.func
            callee, kind = None, None
            if isinstance(func, ast.Name):
                callee, kind = func.id, "name"          # foo()
            elif isinstance(func, ast.Attribute):
                callee = func.attr
                recv = func.value
                is_super = isinstance(recv, ast.Call) and _expr_name(recv.func) == "super"
                if is_super or (isinstance(recv, ast.Name) and recv.id in ("self", "cls")):
                    kind = "self"                       # self.foo() / cls.foo() / super().foo()
                else:
                    kind = "attr"                       # something.foo(): receiver type unknown
            if callee:
                self.calls.append((caller, callee, kind, node.lineno))
                if callee in DYNAMIC_CALLS:
                    self._dynamic[caller].add(callee)

            # Functions passed as values (callbacks, Thread(target=f), sorted(key=f)...)
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Name):
                    self.calls.append((caller, arg.id, "ref", arg.lineno))
                elif (
                    isinstance(arg, ast.Attribute)
                    and isinstance(arg.value, ast.Name)
                    and arg.value.id in ("self", "cls")
                ):
                    self.calls.append((caller, arg.attr, "ref", arg.lineno))
        self.generic_visit(node)

    # ---- imports ----
    def visit_Import(self, node):
        for alias in node.names:
            self.imports.append({"module": alias.name, "level": 0, "names": [], "lineno": node.lineno})

    def visit_ImportFrom(self, node):
        self.imports.append(
            {
                "module": node.module or "",
                "level": node.level,
                "names": [a.name for a in node.names],
                "lineno": node.lineno,
            }
        )

    def finalize(self):
        """Fill in the blind-spot flags once the whole file has been visited."""
        for parent, nested_names in self._nested.items():
            returned = self._returned.get(parent, set())
            escaping = nested_names & returned
            if escaping or "<lambda>" in returned:
                self._by_qual[parent]["returns_callable"] = True
            for name in escaping:
                child = self._by_qual.get(f"{parent}.{name}")
                if child:
                    child["escapes"] = True
        for qual, feats in self._dynamic.items():
            self._by_qual[qual]["dynamic"] = ",".join(sorted(feats))


def parse_file(path: Path, root: Path) -> dict | None:
    """Parse one file. Returns None if it cannot be parsed (e.g. syntax error)."""
    rel = path.relative_to(root).as_posix()
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None

    collector = _Collector()
    collector.visit(tree)
    collector.finalize()
    parts = rel.lower().split("/")
    return {
        "path": rel,
        "loc": src.count("\n") + 1,
        "doc": _doc_line(tree),
        "is_test": any(p.startswith("test") or p == "tests" for p in parts),
        "functions": collector.functions,
        "classes": collector.classes,
        "imports": collector.imports,
        "calls": collector.calls,
    }


def parse_repo(root: Path) -> list[dict]:
    """Parse every Python file under root. Prints a note for files it had to skip."""
    parsed, skipped = [], 0
    for path in iter_python_files(root):
        result = parse_file(path, root)
        if result is None:
            skipped += 1
        else:
            parsed.append(result)
    if skipped:
        print(f"  (skipped {skipped} file(s) that could not be parsed)")
    return parsed
