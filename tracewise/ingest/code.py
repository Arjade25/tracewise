"""Static code layer: parse a Python repository with ``ast`` into a call graph.

Nodes:  Module, Class, Function
Edges:  (Module)-[:DEFINES]->(Class|Function)
        (Class)-[:HAS_METHOD]->(Function)
        (Class)-[:INHERITS]->(Class)
        (Module)-[:IMPORTS]->(Module)
        (Function)-[:CALLS {confidence}]->(Function)
        (Function)-[:INSTANTIATES {confidence}]->(Class)

Call resolution is best-effort but type-aware: it follows imports and re-exports,
``self``/``cls``/``super()``, parameter and variable annotations, and constructor
assignments. Calls it cannot pin down exactly fall back to a unique method-name
match and are marked ``confidence: 'heuristic'`` so the agent can say how sure it is.
"""
from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__", ".tox", "docs"}
BUILTINS = set(dir(builtins))
# Method names too generic to resolve by name alone.
GENERIC = {
    "get", "set", "update", "pop", "append", "extend", "items", "keys", "values", "copy", "join",
    "split", "format", "read", "write", "close", "open", "run", "send", "add", "remove", "clear",
    "encode", "decode", "strip", "replace", "startswith", "endswith", "lower", "upper", "setdefault",
    "insert", "index", "count", "sort", "find", "match", "load", "loads", "dump", "dumps", "call",
    "__init__", "__call__", "__enter__", "__exit__", "__getattr__", "__getitem__", "__setitem__",
    "__eq__", "__repr__", "__str__", "__len__", "__iter__", "__contains__", "__bool__", "__hash__",
}


@dataclass
class FuncInfo:
    qname: str
    name: str
    module: str
    path: str
    line: int
    end_line: int
    cls: str | None
    doc: str
    is_test: bool
    is_async: bool
    signature: str
    node: ast.AST = field(repr=False, default=None)


@dataclass
class ClassInfo:
    qname: str
    name: str
    module: str
    path: str
    line: int
    end_line: int
    doc: str
    base_exprs: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    methods: dict[str, str] = field(default_factory=dict)  # name -> qname
    attr_types: dict[str, str] = field(default_factory=dict)  # self.x annotated types


@dataclass
class ModuleInfo:
    name: str
    path: str
    is_package: bool
    is_test: bool
    loc: int
    doc: str
    tree: ast.Module = field(repr=False, default=None)
    bindings: dict[str, str] = field(default_factory=dict)  # local name -> dotted target
    imports: set[str] = field(default_factory=set)
    symbols: dict[str, str] = field(default_factory=dict)  # local def name -> qname


def _module_name(rel: Path, src_roots: list[Path]) -> str:
    for root in src_roots:
        try:
            r = rel.relative_to(root)
            break
        except ValueError:
            continue
    else:
        r = rel
    parts = list(r.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _dotted(expr: ast.AST) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = _dotted(expr.value)
        return f"{base}.{expr.attr}" if base else None
    if isinstance(expr, ast.Subscript):  # Optional[Foo] / list[Foo] -> best effort inner
        return _dotted(expr.slice) if not isinstance(expr.slice, ast.Tuple) else None
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):  # "Foo" forward refs
        return expr.value.strip("'\"") or None
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):  # Foo | None
        return _dotted(expr.left) or _dotted(expr.right)
    return None


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(node.args)
    except Exception:
        args = "..."
    ret = ""
    if node.returns is not None:
        try:
            ret = " -> " + ast.unparse(node.returns)
        except Exception:
            pass
    return f"{node.name}({args}){ret}"[:300]


class CodeIndex:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.modules: dict[str, ModuleInfo] = {}
        self.classes: dict[str, ClassInfo] = {}
        self.funcs: dict[str, FuncInfo] = {}
        self.calls: dict[tuple[str, str], str] = {}  # (caller, callee) -> confidence
        self.instantiates: dict[tuple[str, str], str] = {}
        self.by_method_name: dict[str, list[str]] = {}

    # ---------------------------------------------------------------- parsing
    def scan(self) -> "CodeIndex":
        files = [
            p for p in self.root.rglob("*.py")
            if not any(part in SKIP_DIRS or part.startswith(".") or part == "site-packages"
                       for part in p.relative_to(self.root).parts[:-1])
        ]
        src_roots = [Path("src")] if (self.root / "src").is_dir() else []
        for p in sorted(files):
            rel = p.relative_to(self.root)
            try:
                text = p.read_text(encoding="utf-8")
                tree = ast.parse(text)
            except (SyntaxError, UnicodeDecodeError):
                continue
            name = _module_name(rel, src_roots)
            if not name:
                continue
            is_test = rel.parts[0] in {"tests", "test"} or rel.name.startswith("test_")
            mod = ModuleInfo(
                name=name, path=rel.as_posix(), is_package=p.name == "__init__.py",
                is_test=is_test, loc=text.count("\n") + 1,
                doc=(ast.get_docstring(tree) or "")[:500], tree=tree,
            )
            self.modules[name] = mod
        for mod in self.modules.values():
            self._collect(mod)
        for mod in self.modules.values():
            self._collect_imports(mod)
        for cls in self.classes.values():
            mod = self.modules[cls.module]
            cls.bases = [b for b in (self.resolve(mod, e) for e in cls.base_exprs) if b in self.classes]
        for f in self.funcs.values():
            if f.cls:
                self.by_method_name.setdefault(f.name, []).append(f.qname)
        for f in self.funcs.values():
            self._collect_calls(f)
        return self

    def _collect(self, mod: ModuleInfo) -> None:
        for node in mod.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{mod.name}.{node.name}"
                mod.symbols[node.name] = q
                self._add_func(mod, node, q, None)
            elif isinstance(node, ast.ClassDef):
                q = f"{mod.name}.{node.name}"
                mod.symbols[node.name] = q
                cls = ClassInfo(
                    qname=q, name=node.name, module=mod.name, path=mod.path, line=node.lineno,
                    end_line=node.end_lineno or node.lineno, doc=(ast.get_docstring(node) or "")[:500],
                    base_exprs=[d for d in (_dotted(b) for b in node.bases) if d],
                )
                self.classes[q] = cls
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        mq = f"{q}.{item.name}"
                        cls.methods[item.name] = mq
                        self._add_func(mod, item, mq, q)
                    elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        t = _dotted(item.annotation)
                        if t:
                            cls.attr_types[item.target.id] = t
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        mod.symbols.setdefault(t.id, f"{mod.name}.{t.id}")

    def _add_func(self, mod, node, qname, cls):
        is_test = mod.is_test and node.name.startswith("test")
        self.funcs[qname] = FuncInfo(
            qname=qname, name=node.name, module=mod.name, path=mod.path, line=node.lineno,
            end_line=node.end_lineno or node.lineno, cls=cls,
            doc=(ast.get_docstring(node) or "")[:500], is_test=is_test,
            is_async=isinstance(node, ast.AsyncFunctionDef), signature=_signature(node), node=node,
        )
        # self.x: Foo = ... inside __init__ etc. gives attribute types
        if cls:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Attribute)
                        and isinstance(sub.target.value, ast.Name) and sub.target.value.id == "self"):
                    t = _dotted(sub.annotation)
                    if t:
                        self.classes[cls].attr_types.setdefault(sub.target.attr, t)

    def _collect_imports(self, mod: ModuleInfo) -> None:
        pkg_parts = mod.name.split(".") if mod.is_package else mod.name.split(".")[:-1]
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        mod.bindings[a.asname] = a.name
                    else:
                        top = a.name.split(".")[0]
                        mod.bindings.setdefault(top, top)
                    if a.name in self.modules:
                        mod.imports.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base_parts = pkg_parts[: len(pkg_parts) - (node.level - 1)] if node.level > 1 else pkg_parts
                    base = ".".join(base_parts + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                if base in self.modules:
                    mod.imports.add(base)
                for a in node.names:
                    if a.name == "*":
                        continue
                    target = f"{base}.{a.name}" if base else a.name
                    mod.bindings[a.asname or a.name] = target
                    if target in self.modules:
                        mod.imports.add(target)

    # ------------------------------------------------------------- resolution
    def canonical(self, dotted: str, depth: int = 0) -> str | None:
        """Follow re-exports until we reach a real Module/Class/Function qname."""
        if dotted in self.funcs or dotted in self.classes or dotted in self.modules:
            return dotted
        if depth > 6 or "." not in dotted:
            return None
        head, _, last = dotted.rpartition(".")
        owner = self.canonical(head, depth + 1)
        if owner is None:
            return None
        if owner in self.modules:
            mod = self.modules[owner]
            if last in mod.symbols and mod.symbols[last] != f"{owner}.{last}":
                return self.canonical(mod.symbols[last], depth + 1)
            if f"{owner}.{last}" in self.funcs or f"{owner}.{last}" in self.classes:
                return f"{owner}.{last}"
            if last in mod.bindings:
                return self.canonical(mod.bindings[last], depth + 1)
            if f"{owner}.{last}" in self.modules:
                return f"{owner}.{last}"
            return None
        if owner in self.classes:
            return self.lookup_method(owner, last)
        return None

    def resolve(self, mod: ModuleInfo, dotted: str) -> str | None:
        head, _, rest = dotted.partition(".")
        if head in mod.symbols:
            base = mod.symbols[head]
        elif head in mod.bindings:
            base = mod.bindings[head]
        else:
            return None
        return self.canonical(f"{base}.{rest}" if rest else base)

    def lookup_method(self, cls_q: str, name: str, seen=None) -> str | None:
        seen = seen or set()
        if cls_q in seen or cls_q not in self.classes:
            return None
        seen.add(cls_q)
        cls = self.classes[cls_q]
        if name in cls.methods:
            return cls.methods[name]
        for b in cls.bases:
            r = self.lookup_method(b, name, seen)
            if r:
                return r
        return None

    def attr_type(self, cls_q: str, name: str, seen=None) -> str | None:
        seen = seen or set()
        if cls_q in seen or cls_q not in self.classes:
            return None
        seen.add(cls_q)
        cls = self.classes[cls_q]
        if name in cls.attr_types:
            return self.resolve(self.modules[cls.module], cls.attr_types[name])
        for b in cls.bases:
            r = self.attr_type(b, name, seen)
            if r:
                return r
        return None

    def _collect_calls(self, f: FuncInfo) -> None:
        mod = self.modules[f.module]
        node = f.node
        # local variable -> class qname, seeded from annotated parameters
        local_types: dict[str, str] = {}
        all_args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        for a in all_args:
            if a.annotation is not None:
                t = _dotted(a.annotation)
                r = self.resolve(mod, t) if t else None
                if r in self.classes:
                    local_types[a.arg] = r
        if f.cls:
            local_types.setdefault("self", f.cls)
            local_types.setdefault("cls", f.cls)

        def type_of(expr) -> str | None:
            if isinstance(expr, ast.Name):
                return local_types.get(expr.id)
            if isinstance(expr, ast.Attribute):
                owner = type_of(expr.value)
                if owner:
                    t = self.attr_type(owner, expr.attr)
                    if t in self.classes:
                        return t
            if isinstance(expr, ast.Call):
                d = _dotted(expr.func)
                r = self.resolve(mod, d) if d else None
                if r in self.classes:
                    return r
            return None

        for sub in ast.walk(node):
            if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                tgt = sub.targets[0] if isinstance(sub, ast.Assign) else sub.target
                if isinstance(tgt, ast.Name):
                    t = None
                    if isinstance(sub, ast.AnnAssign):
                        d = _dotted(sub.annotation)
                        t = self.resolve(mod, d) if d else None
                    if t not in self.classes and sub.value is not None:
                        t = type_of(sub.value)
                    if t in self.classes:
                        local_types[tgt.id] = t

        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            target, conf = None, "exact"
            if isinstance(fn, ast.Name):
                if fn.id in mod.symbols or fn.id in mod.bindings:
                    target = self.resolve(mod, fn.id)
                elif fn.id in BUILTINS:
                    continue
            elif isinstance(fn, ast.Attribute):
                v = fn.value
                if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "super"
                        and f.cls):
                    for b in self.classes[f.cls].bases:
                        target = self.lookup_method(b, fn.attr)
                        if target:
                            break
                else:
                    owner = type_of(v)
                    if owner:
                        target = self.lookup_method(owner, fn.attr)
                    else:
                        d = _dotted(fn)
                        if d:
                            target = self.resolve(mod, d)
                    if target is None and fn.attr not in GENERIC and not fn.attr.startswith("__"):
                        cands = [c for c in self.by_method_name.get(fn.attr, []) if not self.funcs[c].is_test]
                        if 1 <= len(cands) <= 2:
                            for c in cands:
                                if c != f.qname:
                                    self.calls.setdefault((f.qname, c), "heuristic")
                            continue
            if not target or target == f.qname:
                continue
            if target in self.funcs:
                prev = self.calls.get((f.qname, target))
                if prev != "exact":
                    self.calls[(f.qname, target)] = conf
            elif target in self.classes:
                self.instantiates[(f.qname, target)] = conf
                init = self.lookup_method(target, "__init__")
                if init and init != f.qname:
                    self.calls.setdefault((f.qname, init), conf)


# ------------------------------------------------------------------ loading
def load_code(g, index: CodeIndex) -> dict:
    from ..db import batched

    mods = [
        {"name": m.name, "path": m.path, "is_package": m.is_package, "is_test": m.is_test,
         "loc": m.loc, "doc": m.doc}
        for m in index.modules.values()
    ]
    for b in batched(mods):
        g.query(
            """UNWIND $rows AS r
               MERGE (m:Module {path: r.path})
               SET m.name = r.name, m.is_package = r.is_package, m.is_test = r.is_test,
                   m.loc = r.loc, m.doc = r.doc, m.exists = true""",
            {"rows": b},
        )
    classes = [
        {"qname": c.qname, "name": c.name, "path": c.path, "line": c.line, "end_line": c.end_line,
         "doc": c.doc}
        for c in index.classes.values()
    ]
    for b in batched(classes):
        g.query(
            """UNWIND $rows AS r
               MATCH (m:Module {path: r.path})
               MERGE (c:Class {qname: r.qname})
               SET c.name = r.name, c.path = r.path, c.line = r.line, c.end_line = r.end_line, c.doc = r.doc
               MERGE (m)-[:DEFINES]->(c)""",
            {"rows": b},
        )
    funcs = [
        {"qname": f.qname, "name": f.name, "path": f.path, "line": f.line, "end_line": f.end_line,
         "doc": f.doc, "is_test": f.is_test, "is_async": f.is_async, "signature": f.signature,
         "cls": f.cls, "loc": f.end_line - f.line + 1}
        for f in index.funcs.values()
    ]
    for b in batched(funcs):
        g.query(
            """UNWIND $rows AS r
               MERGE (f:Function {qname: r.qname})
               SET f.name = r.name, f.path = r.path, f.line = r.line, f.end_line = r.end_line,
                   f.doc = r.doc, f.is_test = r.is_test, f.is_async = r.is_async,
                   f.signature = r.signature, f.loc = r.loc, f.is_method = r.cls IS NOT NULL
               WITH f, r
               MATCH (m:Module {path: r.path})
               MERGE (m)-[:DEFINES]->(f)""",
            {"rows": b},
        )
    meth = [{"c": f.cls, "f": f.qname} for f in index.funcs.values() if f.cls]
    for b in batched(meth):
        g.query(
            """UNWIND $rows AS r
               MATCH (c:Class {qname: r.c}), (f:Function {qname: r.f})
               MERGE (c)-[:HAS_METHOD]->(f)""",
            {"rows": b},
        )
    inh = [{"a": c.qname, "b": b} for c in index.classes.values() for b in c.bases]
    if inh:
        g.query(
            """UNWIND $rows AS r
               MATCH (a:Class {qname: r.a}), (b:Class {qname: r.b})
               MERGE (a)-[:INHERITS]->(b)""",
            {"rows": inh},
        )
    imps = [
        {"a": m.path, "b": index.modules[t].path}
        for m in index.modules.values() for t in m.imports if t in index.modules and t != m.name
    ]
    for b in batched(imps):
        g.query(
            """UNWIND $rows AS r
               MATCH (a:Module {path: r.a}), (b:Module {path: r.b})
               MERGE (a)-[:IMPORTS]->(b)""",
            {"rows": b},
        )
    calls = [{"a": a, "b": b, "c": c} for (a, b), c in index.calls.items()]
    for b in batched(calls, 1000):
        g.query(
            """UNWIND $rows AS r
               MATCH (a:Function {qname: r.a}), (b:Function {qname: r.b})
               MERGE (a)-[e:CALLS]->(b) SET e.confidence = r.c""",
            {"rows": b},
        )
    inst = [{"a": a, "b": b, "c": c} for (a, b), c in index.instantiates.items()]
    for b in batched(inst, 1000):
        g.query(
            """UNWIND $rows AS r
               MATCH (a:Function {qname: r.a}), (b:Class {qname: r.b})
               MERGE (a)-[e:INSTANTIATES]->(b) SET e.confidence = r.c""",
            {"rows": b},
        )
    return {
        "modules": len(mods), "classes": len(classes), "functions": len(funcs),
        "calls": len(calls), "calls_exact": sum(1 for c in index.calls.values() if c == "exact"),
        "instantiates": len(inst), "imports": len(imps), "inherits": len(inh),
    }
