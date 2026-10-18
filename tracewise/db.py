"""FalkorDB connection helpers.

Tracewise uses FalkorDB's multigraph support: every indexed repository lives in
its own isolated graph (``repo:<name>``) and every user gets a private memory
graph (``memory:<user>``), all inside one FalkorDB instance.
"""
from __future__ import annotations

import os
from functools import lru_cache

from falkordb import FalkorDB, Graph

REPO_PREFIX = "repo:"
MEMORY_PREFIX = "memory:"


@lru_cache(maxsize=1)
def client() -> FalkorDB:
    url = os.environ.get("FALKORDB_URL")
    if url:
        return FalkorDB.from_url(url)
    return FalkorDB(
        host=os.environ.get("FALKORDB_HOST", "localhost"),
        port=int(os.environ.get("FALKORDB_PORT", "6379")),
        password=os.environ.get("FALKORDB_PASSWORD") or None,
    )


def repo_graph(repo: str) -> Graph:
    return client().select_graph(REPO_PREFIX + repo)


def memory_graph(user: str) -> Graph:
    return client().select_graph(MEMORY_PREFIX + user)


def list_repos() -> list[str]:
    return sorted(g[len(REPO_PREFIX):] for g in client().list_graphs() if g.startswith(REPO_PREFIX))


def list_users() -> list[str]:
    """Everyone who has a memory graph (one per user)."""
    return sorted(g[len(MEMORY_PREFIX):] for g in client().list_graphs() if g.startswith(MEMORY_PREFIX))


REPO_INDEXES = [
    ("Module", "path"),
    ("Module", "name"),
    ("Class", "qname"),
    ("Class", "name"),
    ("Function", "qname"),
    ("Function", "name"),
    ("Person", "email"),
    ("Person", "name"),
    ("Commit", "sha"),
    ("PR", "number"),
    ("Issue", "number"),
    ("Release", "version"),
]


def ensure_repo_indexes(g: Graph) -> None:
    for label, prop in REPO_INDEXES:
        try:
            g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception as exc:  # index already exists
            if "already" not in str(exc).lower():
                raise
    try:
        g.query("CALL db.idx.fulltext.createNodeIndex('Function', 'name', 'doc')")
    except Exception:
        pass


def rows(result) -> list[dict]:
    """Turn a FalkorDB result set into a list of dicts keyed by column alias."""
    header = [h[1] if isinstance(h, (list, tuple)) else h for h in result.header]
    header = [h.decode() if isinstance(h, bytes) else h for h in header]
    return [dict(zip(header, r)) for r in result.result_set]


def batched(items: list, size: int = 500):
    for i in range(0, len(items), size):
        yield items[i : i + size]
