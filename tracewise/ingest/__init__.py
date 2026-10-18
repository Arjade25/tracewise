"""Build a repository graph in FalkorDB."""
from __future__ import annotations

import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from ..db import ensure_repo_indexes, repo_graph, rows
from .code import CodeIndex, load_code
from .history import load_history


@contextmanager
def checkout(repo: Path, rev: str | None):
    """Yield a working tree at ``rev`` (a temporary git worktree), or the repo itself."""
    if not rev:
        yield repo
        return
    tmp = Path(tempfile.mkdtemp(prefix="tracewise-rev-")) / "tree"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", "--quiet", str(tmp), rev],
                   check=True, capture_output=True)
    try:
        yield tmp
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tmp)], capture_output=True)


def ingest(repo_path: str | Path, name: str | None = None, *, reset: bool = True, rev: str | None = None,
           max_commits: int | None = None, coverage_python: str | None = None, log=print) -> dict:
    """Build (or rebuild) the graph for a repository, optionally as of a past revision ``rev``."""
    src = Path(repo_path).resolve()
    name = name or src.name
    with checkout(src, rev) as repo:
        return _ingest(repo, src, name, rev, reset=reset, max_commits=max_commits,
                       coverage_python=coverage_python, log=log)


def _ingest(repo: Path, src: Path, name: str, rev: str | None, *, reset: bool, max_commits, coverage_python,
            log) -> dict:
    g = repo_graph(name)
    if reset:
        try:
            g.delete()
        except Exception:
            pass
        g = repo_graph(name)
    ensure_repo_indexes(g)
    t0 = time.time()
    index = CodeIndex(repo).scan()
    log(f"parsed {len(index.modules)} modules in {time.time() - t0:.1f}s")
    stats = {"code": load_code(g, index)}
    log(f"code layer: {stats['code']}")
    stats["history"] = load_history(g, repo, index, max_commits=max_commits)
    log(f"history layer: {stats['history']}")
    if coverage_python:
        from .coverage import load_coverage
        try:
            stats["coverage"] = load_coverage(g, repo, index, python=coverage_python)
            log(f"coverage layer: {stats['coverage']}")
        except Exception as exc:  # the graph is still useful without it
            log(f"coverage layer skipped: {exc}")
    head = rows(g.query("MATCH (c:Commit) RETURN max(c.ts) AS ts"))[0]["ts"]
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    g.query("""MERGE (r:Repo {name: $n}) SET r.path = $p, r.indexed_at = timestamp(), r.head_ts = $h,
               r.rev = $rev, r.head_sha = $sha""",
            {"n": name, "p": str(src), "h": head, "rev": rev or "HEAD", "sha": sha})
    log(f"indexed {name} at {rev or 'HEAD'} ({sha})")
    stats["seconds"] = round(time.time() - t0, 1)
    log(f"done in {stats['seconds']}s")
    return stats
