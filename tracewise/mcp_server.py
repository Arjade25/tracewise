"""MCP server: plug the Tracewise graph into Claude Desktop, Claude Code, Cursor, etc.

    python -m tracewise.mcp_server --repo flask --user me

Every tool reads FalkorDB; results include the Cypher that produced them. Tool calls
are also written to the user's memory graph, so the host agent gets the same
cross-session memory as the built-in agent.
"""
from __future__ import annotations

import argparse
import json
import os

from mcp.server.fastmcp import FastMCP

from . import tools as T
from .agent import facts_from
from .crew import review as crew_review
from .db import list_repos
from .memory import Memory

mcp = FastMCP("tracewise")
STATE = {"repo": os.environ.get("TRACEWISE_REPO", "flask"), "user": os.environ.get("TRACEWISE_USER", "mcp")}
_mem: dict = {}


def _memory() -> tuple[Memory, str]:
    if "m" not in _mem:
        _mem["m"] = Memory(STATE["user"])
        _mem["s"] = _mem["m"].start_session(STATE["repo"])
    return _mem["m"], _mem["s"]


def _run(name: str, **args) -> str:
    res = T.TOOLS[name](STATE["repo"], **args)
    mem, session = _memory()
    for f in facts_from(res):
        mem.remember(STATE["repo"], f["key"], f["kind"], f["text"], f["value"], source="mcp:" + name,
                     label=f.get("label"), name=T.short(f["key"]))
    mem.record_turn(session, f"[mcp] {name} {json.dumps(args)[:200]}", json.dumps(res["result"], default=str)[:800],
                    [res], intent=name)
    return json.dumps({"result": res["result"], "graph_path": res["evidence"]["edges"][:40],
                       "cypher": res["cypher"]}, default=str, indent=1)[:60000]


@mcp.tool()
def impact(symbol: str, depth: int = 3) -> str:
    """Blast radius of changing a function, class or module (transitive callers, risk, tests)."""
    return _run("impact", symbol=symbol, depth=depth)


@mcp.tool()
def tests_for(symbol: str) -> str:
    """Tests that execute a symbol (measured coverage + static call paths) and a pytest command."""
    return _run("tests_for", symbol=symbol)


@mcp.tool()
def experts(symbol: str) -> str:
    """Who knows this code best, with evidence (authorship, nearby code, recent commits, merged PRs)."""
    return _run("experts", symbol=symbol)


@mcp.tool()
def why(symbol: str) -> str:
    """Decision trace: commits -> PRs -> issues -> release notes that shaped this code."""
    return _run("why", symbol=symbol)


@mcp.tool()
def connection(a: str, b: str) -> str:
    """Shortest structural path between two pieces of code."""
    return _run("connection", a=a, b=b)


@mcp.tool()
def hotspots(limit: int = 10) -> str:
    """Riskiest code: PageRank over the call graph x churn x number of authors."""
    return _run("hotspots", limit=limit)


@mcp.tool()
def bridges(limit: int = 10) -> str:
    """Chokepoint functions by betweenness centrality."""
    return _run("bridges", limit=limit)


@mcp.tool()
def find_symbol(query: str) -> str:
    """Search functions and classes by (partial) name."""
    return _run("find_symbol", query=query)


@mcp.tool()
def person_profile(person: str) -> str:
    """What a contributor owns and works on."""
    return _run("person_profile", person=person)


@mcp.tool()
def review_change(diff: str = "", symbols: list[str] | None = None) -> str:
    """Review plan for a change (unified diff or function names): risk, reviewers, tests, past decisions."""
    return _run("review_change", diff=diff or None, symbols=symbols or [])


@mcp.tool()
def crew_review_change(diff: str = "", symbols: list[str] | None = None) -> str:
    """Same review, done by a crew of agents that coordinate through the shared memory graph."""
    r = crew_review(STATE["repo"], STATE["user"], symbols=symbols or [], diff=diff or None)
    r.pop("log", None)
    return json.dumps(r, default=str, indent=1)[:60000]


@mcp.tool()
def run_cypher(query: str) -> str:
    """Read-only Cypher against the repo graph (see graph_schema for labels and relationships)."""
    return _run("run_cypher", query=query)


@mcp.tool()
def graph_schema() -> str:
    """Node labels and relationship types in the repo graph, with counts."""
    return _run("graph_schema")


@mcp.tool()
def recall(symbol: str) -> str:
    """What this user's memory holds about a symbol: facts over time and earlier questions."""
    mem, _ = _memory()
    sym = T.resolve(T.Trace(STATE["repo"]), symbol) or {}
    key = sym.get("key", symbol)
    return json.dumps({"facts": mem.fact_timeline(key), "earlier": mem.history_about([key])}, default=str, indent=1)


@mcp.tool()
def remember(symbol: str, note: str) -> str:
    """Store a note about a symbol in this user's memory (e.g. a team convention or decision)."""
    mem, _ = _memory()
    sym = T.resolve(T.Trace(STATE["repo"]), symbol) or {}
    r = mem.remember(STATE["repo"], sym.get("key", symbol), "note", note, source="mcp:user",
                     label=sym.get("label"), name=T.short(sym.get("key", symbol)))
    return json.dumps(r)


@mcp.tool()
def use_repo(name: str) -> str:
    """Switch which indexed repository the tools query."""
    repos = list_repos()
    if name not in repos:
        return f"unknown repo {name!r}; indexed: {repos}"
    STATE["repo"] = name
    _mem.clear()
    return f"now using {name}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=STATE["repo"])
    ap.add_argument("--user", default=STATE["user"])
    ap.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    a = ap.parse_args()
    STATE.update(repo=a.repo, user=a.user)
    mcp.run(transport=a.transport)


if __name__ == "__main__":
    main()
