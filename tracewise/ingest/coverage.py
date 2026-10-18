"""Optional dynamic layer: which tests actually execute which functions.

Runs the target repo's pytest suite under coverage.py with per-test dynamic
contexts, then maps each executed line back to the function that contains it.

Edges:  (Function {is_test:true})-[:COVERS {lines}]->(Function)

Static call resolution can't see calls that go through frameworks (a test client
calling into a WSGI app, say); this layer fills those gaps with ground truth.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from .code import CodeIndex

RC = """[run]
dynamic_context = test_function
relative_files = true
source = {source}
data_file = {data}
"""


def run_coverage(repo: Path, python: str | None = None, source: str = "src", tests: str = "tests",
                 timeout: int = 1800) -> Path:
    python = python or sys.executable
    tmp = Path(tempfile.mkdtemp(prefix="tracewise-cov-"))
    data = tmp / ".coverage"
    rc = tmp / "coveragerc"
    rc.write_text(RC.format(source=source, data=data))
    env = {**os.environ, "COVERAGE_RCFILE": str(rc)}
    # Import the code from this tree (it may be a worktree at a past revision), not an installed copy.
    if (repo / source).is_dir():
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(repo / source), env.get("PYTHONPATH")]))
    subprocess.run(
        [python, "-m", "coverage", "run", "-m", "pytest", "-q", "-p", "no:cacheprovider", tests],
        cwd=repo, env=env, timeout=timeout, capture_output=True, text=True,
    )
    if not data.exists():
        raise RuntimeError("coverage produced no data; is pytest + coverage installed for this repo?")
    return data


def covers_edges(data_file: Path, index: CodeIndex) -> list[dict]:
    from coverage import CoverageData

    cov = CoverageData(basename=str(data_file))
    cov.read()
    by_path = defaultdict(list)
    for f in index.funcs.values():
        if not f.is_test:
            by_path[f.path].append(f)
    # coverage contexts are "test_basic.test_session" or "test_x.TestY.test_z": the test's
    # qname minus however much of the package prefix pytest's rootdir dropped.
    tests_by_name: dict[str, str] = {}
    for f in index.funcs.values():
        if f.is_test:
            parts = f.qname.split(".")
            for i in range(len(parts) - 1):
                tests_by_name.setdefault(".".join(parts[i:]), f.qname)
    edges: dict[tuple[str, str], int] = defaultdict(int)
    for measured in cov.measured_files():
        rel = Path(measured).as_posix()
        funcs = by_path.get(rel) or next((v for k, v in by_path.items() if rel.endswith(k)), [])
        if not funcs:
            continue
        ctx_by_line = cov.contexts_by_lineno(measured)
        for f in funcs:
            for ln in range(f.line + 1, f.end_line + 1):  # skip the def line (runs at import)
                for ctx in ctx_by_line.get(ln, []):
                    test = tests_by_name.get(ctx.split("|")[0]) if ctx else None
                    if test:
                        edges[(test, f.qname)] += 1
    return [{"t": t, "f": f, "n": n} for (t, f), n in edges.items()]


def load_coverage(g, repo: Path, index: CodeIndex, python: str | None = None) -> dict:
    from ..db import batched

    data = run_coverage(repo, python=python)
    edges = covers_edges(data, index)
    for b in batched(edges, 2000):
        g.query(
            """UNWIND $rows AS r MATCH (t:Function {qname: r.t}), (f:Function {qname: r.f})
               MERGE (t)-[e:COVERS]->(f) SET e.lines = r.n""",
            {"rows": b},
        )
    return {"covers": len(edges), "tests": len({e["t"] for e in edges}), "functions": len({e["f"] for e in edges})}
