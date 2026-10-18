"""Graph tools the agent calls. Every tool answers from FalkorDB and returns:

* ``result``   – the structured answer
* ``evidence`` – the nodes and edges the answer was derived from (rendered in the UI)
* ``cypher``   – the exact queries that ran, so every answer is reproducible

Nothing here is computed outside the graph: remove FalkorDB and none of it works.
"""
from __future__ import annotations

import re
from pathlib import Path
import time
from collections import defaultdict
from typing import Any

from .db import repo_graph, rows

BOT_RE = re.compile(r"\[bot\]|pre-commit|dependabot|github-actions", re.I)


class Trace:
    """Collects the queries run and the subgraph used for one tool call."""

    def __init__(self, repo: str):
        self.repo = repo
        self.g = repo_graph(repo)
        self.cypher: list[str] = []
        self.nodes: dict[str, dict] = {}
        self.edges: set[tuple[str, str, str]] = set()

    def q(self, query: str, params: dict | None = None) -> list[dict]:
        self.cypher.append(" ".join(query.split()))
        return rows(self.g.ro_query(query, params or {}))

    def node(self, nid: str, label: str, name: str | None = None, **props) -> str:
        n = self.nodes.setdefault(nid, {"id": nid, "label": label, "name": name or nid})
        n.update({k: v for k, v in props.items() if v is not None})
        return nid

    def edge(self, a: str, b: str, rel: str) -> None:
        self.edges.add((a, b, rel))

    def done(self, tool: str, args: dict, result: Any) -> dict:
        return {
            "tool": tool, "args": args, "result": result,
            "evidence": {"nodes": list(self.nodes.values()),
                         "edges": [{"source": a, "target": b, "type": t} for a, b, t in sorted(self.edges)]},
            "cypher": self.cypher,
        }


def short(qname: str) -> str:
    parts = qname.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else qname


# ----------------------------------------------------------------- resolve
def resolve(t: Trace, name: str) -> dict | None:
    """Map what a human types ("full_dispatch_request", "Flask", "app.py") to a node."""
    name = name.strip().strip("`").removesuffix("()")
    hit = t.q(
        """MATCH (n) WHERE (n:Function OR n:Class) AND n.qname = $n
           RETURN labels(n)[0] AS label, n.qname AS key, n.path AS path LIMIT 1""",
        {"n": name},
    )
    if hit:
        return hit[0]
    hit = t.q(
        """MATCH (m:Module) WHERE m.exists = true AND (m.path = $n OR m.name = $n OR m.path ENDS WITH ('/' + $n))
           RETURN 'Module' AS label, m.path AS key, m.path AS path ORDER BY size(m.path) LIMIT 1""",
        {"n": name},
    )
    if hit:
        return hit[0]
    # Several things can share a short name ("register"). Prefer library code over tests and
    # examples, then the most-used candidate (fan-in), and report the others as alternatives.
    hit = t.q(
        """MATCH (n) WHERE (n:Function OR n:Class) AND (n.qname ENDS WITH ('.' + $n))
           OPTIONAL MATCH (n)<-[c:CALLS|INSTANTIATES]-(caller:Function)
           WHERE c.confidence = 'exact' AND NOT coalesce(caller.is_test, false)
           WITH n, count(c) AS fan_in
           RETURN labels(n)[0] AS label, n.qname AS key, n.path AS path,
                  coalesce(n.is_test, false) OR n.path STARTS WITH 'tests/' AS test,
                  n.path STARTS WITH 'examples/' OR n.path STARTS WITH 'docs/' AS example, fan_in
           ORDER BY test, example, fan_in DESC, size(n.qname) LIMIT 6""",
        {"n": name},
    )
    if not hit:
        return None
    best = dict(hit[0])
    best["alternatives"] = [h["key"] for h in hit[1:]]
    for k in ("test", "example", "fan_in"):
        best.pop(k, None)
    return best


_NAMES: dict[str, tuple[float, list[str]]] = {}


def suggest(repo: str, name: str, n: int = 5) -> list[str]:
    """Closest real names for something that didn't resolve (typos, renamed or deleted code)."""
    import difflib

    cached = _NAMES.get(repo)
    if not cached or time.time() - cached[0] > 300:
        names = [r["q"] for r in Trace(repo).q(
            "MATCH (n) WHERE (n:Function OR n:Class) AND NOT coalesce(n.is_test, false) RETURN n.qname AS q")]
        _NAMES[repo] = cached = (time.time(), names)
    names = cached[1]
    short = {}
    for q in names:
        short.setdefault(q.rsplit(".", 1)[-1], q)
    leaf = name.strip("`").removesuffix("()").rsplit(".", 1)[-1]
    close = difflib.get_close_matches(leaf, list(short), n=n, cutoff=0.6)
    contains = [k for k in short if leaf.lower() in k.lower() and k not in close][: max(0, n - len(close))]
    return [short[k] for k in close + contains]


def find_symbol(repo: str, query: str, limit: int = 10) -> dict:
    t = Trace(repo)
    res = t.q(
        """MATCH (n) WHERE (n:Function OR n:Class) AND toLower(n.qname) CONTAINS toLower($q)
           RETURN labels(n)[0] AS kind, n.qname AS qname, n.path AS path, n.line AS line,
                  coalesce(n.signature, '') AS signature, coalesce(n.is_test, false) AS test,
                  left(coalesce(n.doc, ''), 160) AS doc
           ORDER BY test, size(n.qname) LIMIT $limit""",
        {"q": query, "limit": limit},
    )
    for r in res:
        t.node(r["qname"], r["kind"], short(r["qname"]), path=r["path"])
    return t.done("find_symbol", {"query": query}, res)


def graph_schema(repo: str) -> dict:
    t = Trace(repo)
    labels = t.q("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC")
    rels = t.q("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY count DESC")
    return t.done("graph_schema", {}, {"labels": labels, "relationships": rels})


# ------------------------------------------------------------------ impact
def _targets(t: Trace, sym: dict) -> list[str]:
    """The functions whose callers count as impacted by changing ``sym``."""
    if sym["label"] == "Function":
        # Calls resolved against a base-class method still dispatch here at runtime.
        over = t.q(
            """MATCH (c:Class)-[:HAS_METHOD]->(f:Function {qname: $q})
               MATCH (c)-[:INHERITS*1..4]->(:Class)-[:HAS_METHOD]->(o:Function {name: f.name})
               RETURN o.qname AS q""",
            {"q": sym["key"]},
        )
        return [sym["key"]] + [o["q"] for o in over]
    if sym["label"] == "Class":
        return [r["q"] for r in t.q(
            "MATCH (:Class {qname: $q})-[:HAS_METHOD]->(f:Function) RETURN f.qname AS q", {"q": sym["key"]})]
    return [r["q"] for r in t.q(
        "MATCH (:Module {path: $p})-[:DEFINES]->(f:Function) RETURN f.qname AS q", {"p": sym["key"]})]


def impact(repo: str, symbol: str, depth: int = 3) -> dict:
    """Blast radius: everything that transitively calls into ``symbol``."""
    t = Trace(repo)
    sym = resolve(t, symbol)
    if not sym:
        return t.done("impact", {"symbol": symbol}, {"error": f"no symbol matching '{symbol}'"})
    depth = max(1, min(int(depth), 5))
    targets = _targets(t, sym)
    t.node(sym["key"], sym["label"], short(sym["key"]), path=sym["path"], role="target")
    res = t.q(
        f"""MATCH (tgt:Function) WHERE tgt.qname IN $targets
            MATCH p = (c:Function)-[:CALLS*1..{depth}]->(tgt)
            WHERE NOT c.qname IN $targets
            WITH c, min(length(p)) AS dist
            RETURN c.qname AS qname, c.path AS path, c.is_test AS test, dist
            ORDER BY dist, qname""",
        {"targets": targets},
    )
    inst = []
    if sym["label"] == "Class":
        inst = t.q(
            """MATCH (f:Function)-[:INSTANTIATES]->(:Class {qname: $q})
               RETURN f.qname AS qname, f.path AS path, f.is_test AS test, 1 AS dist""",
            {"q": sym["key"]},
        )
        sub = t.q("MATCH (s:Class)-[:INHERITS*1..4]->(:Class {qname: $q}) RETURN s.qname AS q", {"q": sym["key"]})
        for s in sub:
            t.node(s["q"], "Class", short(s["q"]))
            t.edge(s["q"], sym["key"], "INHERITS")
    seen = {}
    for r in res + inst:
        if r["qname"] not in seen or seen[r["qname"]]["dist"] > r["dist"]:
            seen[r["qname"]] = r
    callers = sorted(seen.values(), key=lambda r: (r["dist"], r["qname"]))
    prod = [c for c in callers if not c["test"]]
    tests = [c for c in callers if c["test"]]
    # Draw the shortest call chain for the nearest production callers.
    if prod:
        chains = t.q(
            """MATCH (tgt:Function) WHERE tgt.qname IN $targets
               MATCH (c:Function) WHERE c.qname IN $callers
               WITH c, tgt, shortestPath((c)-[:CALLS*1..5]->(tgt)) AS p WHERE p IS NOT NULL
               RETURN [n IN nodes(p) | n.qname] AS chain""",
            {"targets": targets, "callers": [c["qname"] for c in prod[:25]]},
        )
        for ch in chains:
            chain = ch["chain"]
            for a, b in zip(chain, chain[1:]):
                t.node(a, "Function", short(a), role="caller")
                t.node(b, "Function", short(b), role="target" if b in targets else "caller")
                t.edge(a, b, "CALLS")
    covering = _covering_tests(t, targets)
    test_names = list(dict.fromkeys([c["test"] for c in covering] + [c["qname"] for c in tests]))
    for name in test_names[:12]:
        t.node(name, "Test", short(name), role="test")
        t.edge(name, sym["key"], "COVERS")
    tests = [{"qname": n} for n in test_names]
    modules = sorted({c["path"] for c in prod})
    risk = "high" if len(prod) >= 25 or len(modules) >= 6 else "medium" if len(prod) >= 6 else "low"
    return t.done("impact", {"symbol": symbol, "depth": depth}, {
        "target": sym, "also_dispatches_from": targets[1:] if sym["label"] == "Function" else [],
        "risk": risk, "affected_functions": len(prod), "affected_modules": modules,
        "callers": prod[:60], "tests_exercising": [c["qname"] for c in tests][:60],
        "test_count": len(tests),
    })


def tests_for(repo: str, symbol: str) -> dict:
    t = Trace(repo)
    sym = resolve(t, symbol)
    if not sym:
        return t.done("tests_for", {"symbol": symbol}, {"error": f"no symbol matching '{symbol}'"})
    targets = _targets(t, sym)
    t.node(sym["key"], sym["label"], short(sym["key"]), role="target")
    res = _covering_tests(t, targets)
    for r in res[:15]:
        t.node(r["test"], "Test", short(r["test"]), role="test")
        t.edge(r["test"], sym["key"], "COVERS" if r["source"] == "coverage" else "CALLS*")
    files = sorted({r["path"] for r in res})
    pick = sorted(res, key=lambda r: (-r["lines"], r["test"]))[:25]
    cmd = "pytest " + " ".join(_pytest_id(r["test"], r["path"]) for r in pick) if res else None
    return t.done("tests_for", {"symbol": symbol}, {
        "target": sym, "test_count": len(res), "tests": res[:80], "test_files": files,
        "pytest_command": cmd, "untested": not res,
    })


def _covering_tests(t: Trace, targets: list[str]) -> list[dict]:
    """Tests that reach ``targets``: measured coverage first, static call paths second."""
    dyn = t.q(
        """MATCH (tst:Function)-[e:COVERS]->(tgt:Function) WHERE tgt.qname IN $targets
           RETURN tst.qname AS test, tst.path AS path, sum(e.lines) AS lines""",
        {"targets": targets})
    stat = t.q(
        """MATCH (tgt:Function) WHERE tgt.qname IN $targets
           MATCH p = (tst:Function {is_test: true})-[:CALLS*1..4]->(tgt)
           WITH tst, min(length(p)) AS hops
           RETURN tst.qname AS test, tst.path AS path, hops""",
        {"targets": targets})
    out = {r["test"]: {**r, "source": "coverage"} for r in dyn}
    for r in stat:
        out.setdefault(r["test"], {**r, "lines": 0, "source": "static"})
    return sorted(out.values(), key=lambda r: (r["source"] != "coverage", -r["lines"], r["test"]))


def _pytest_id(qname: str, path: str) -> str:
    mod = Path(path).stem
    tail = qname.split(mod + ".", 1)[-1]
    return f"{path}::{tail.replace('.', '::')}"


# ----------------------------------------------------------------- people
def experts(repo: str, symbol: str, limit: int = 5) -> dict:
    """Recommend people to ask about / review this code, with every signal kept separate.

    Signals (each reported on its own, so a reader can weigh them):
      ownership  - surviving lines they wrote in this code (git blame at the indexed revision)
      nearby     - surviving lines they wrote in directly connected functions
      recent     - their non-merge commits to this module in the 3 years before the indexed revision
      merged     - PRs that changed this code and were merged by them (a proxy for review/maintainer role)
    Authorship is not current expertise, so people with no commits anywhere in the repo for over
    2 years before the indexed revision are down-weighted and marked inactive.
    This is a recommendation, not an objective ranking of expertise.
    """
    t = Trace(repo)
    sym = resolve(t, symbol)
    if not sym:
        return t.done("experts", {"symbol": symbol}, {"error": f"no symbol matching '{symbol}'"})
    funcs = [sym["key"]] if sym["label"] == "Function" else _targets(t, sym)
    t.node(sym["key"], sym["label"], short(sym["key"]), role="target")
    head = t.q("MATCH (c:Commit) RETURN max(c.ts) AS ts")[0]["ts"] or int(time.time())
    sig: dict[str, dict] = defaultdict(lambda: {"ownership_lines": 0, "nearby_functions": 0, "nearby_lines": 0,
                                                "recent_commits": 0, "merged_prs": 0})
    own = t.q(
        """MATCH (p:Person)-[w:WROTE]->(f:Function) WHERE f.qname IN $fs
           RETURN p.name AS person, sum(w.lines) AS lines""", {"fs": funcs})
    total = sum(r["lines"] for r in own) or 1
    for r in own:
        sig[r["person"]]["ownership_lines"] = int(r["lines"])
        t.node("person:" + r["person"], "Person", r["person"])
        t.edge("person:" + r["person"], sym["key"], "WROTE")
    for r in t.q(
        """MATCH (f:Function)-[:CALLS]-(n:Function)<-[w:WROTE]-(p:Person)
           WHERE f.qname IN $fs AND NOT n.qname IN $fs AND NOT n.is_test
           RETURN p.name AS person, count(DISTINCT n) AS fns, sum(w.lines) AS lines""", {"fs": funcs}):
        sig[r["person"]].update(nearby_functions=r["fns"], nearby_lines=int(r["lines"]))
    for r in t.q(
        """MATCH (m:Module {path: $path})<-[:MODIFIED]-(c:Commit)<-[:AUTHORED]-(p:Person)
           WHERE c.ts > $since AND NOT c.is_merge
           RETURN p.name AS person, count(c) AS commits""",
        {"path": sym["path"], "since": head - 3 * 365 * 86400}):
        sig[r["person"]]["recent_commits"] = r["commits"]
    for r in t.q(
        """MATCH (f:Function)<-[:CHANGED]-(c:Commit)-[:PART_OF]->(pr:PR)<-[:PART_OF]-(m:Commit)<-[:AUTHORED]-(p:Person)
           WHERE f.qname IN $fs AND m.sha = pr.merge_sha
           RETURN p.name AS person, count(DISTINCT pr) AS prs""", {"fs": funcs}):
        sig[r["person"]]["merged_prs"] = r["prs"]
    people = [p for p in sig if not BOT_RE.search(p)]
    last = {r["person"]: r["last"] for r in t.q(
        """MATCH (p:Person)-[:AUTHORED]->(c:Commit) WHERE p.name IN $people
           RETURN p.name AS person, max(c.ts) AS last""", {"people": people})}

    ranked = []
    for p in people:
        s = sig[p]
        score = (10 * s["ownership_lines"] / total + min(3.0, s["nearby_lines"] / 40)
                 + min(4.0, 0.5 * s["recent_commits"]) + min(3.0, s["merged_prs"]))
        last_ts = last.get(p) or 0
        inactive = head - last_ts > 2 * 365 * 86400
        if inactive:
            score *= 0.35
        ev = []
        if s["ownership_lines"]:
            ev.append(f"wrote {s['ownership_lines']} of the {int(total)} surviving lines of {short(sym['key'])}")
        if s["nearby_functions"]:
            ev.append(f"wrote code in {s['nearby_functions']} directly connected functions")
        if s["recent_commits"]:
            ev.append(f"{s['recent_commits']} commits to {sym['path']} in the 3 years before this revision")
        if s["merged_prs"]:
            ev.append(f"merged {s['merged_prs']} PRs that changed this code")
        ev.append(("inactive: last commit " if inactive else "last commit ")
                  + time.strftime("%Y-%m-%d", time.gmtime(last_ts)) if last_ts else "no commits found")
        ranked.append({"person": p, "score": round(score, 2), "active": not inactive,
                       "last_commit": time.strftime("%Y-%m-%d", time.gmtime(last_ts)) if last_ts else None,
                       "signals": dict(s), "evidence": ev})
    ranked.sort(key=lambda r: -r["score"])
    ranked = ranked[:limit]
    for r in ranked:
        t.node("person:" + r["person"], "Person", r["person"])
    return t.done("experts", {"symbol": symbol}, {
        "target": sym, "experts": ranked, "kind": "recommendation",
        "note": "Ranked by surviving authorship, nearby code, recent module work and merged PRs; "
                "inactive contributors are down-weighted. A recommendation, not a measure of expertise.",
    })


# --------------------------------------------------------------- decisions
def why(repo: str, symbol: str, limit: int = 8) -> dict:
    """Trace a piece of code back to the commits, PRs, issues and release notes behind it."""
    t = Trace(repo)
    sym = resolve(t, symbol)
    if not sym:
        return t.done("why", {"symbol": symbol}, {"error": f"no symbol matching '{symbol}'"})
    funcs = [sym["key"]] if sym["label"] == "Function" else _targets(t, sym)
    t.node(sym["key"], sym["label"], short(sym["key"]), role="target")
    changes = t.q(
        """MATCH (f:Function)<-[e:CHANGED]-(c:Commit)<-[:AUTHORED]-(a:Person) WHERE f.qname IN $fs
           OPTIONAL MATCH (c)-[:PART_OF]->(pr:PR)
           OPTIONAL MATCH (pr)-[:FIXES]->(i:Issue)
           OPTIONAL MATCH (c)-[:FIXES]->(ci:Issue)
           OPTIONAL MATCH (n:ChangeNote)-[:REFERENCES]->(x) WHERE x = pr OR x = i OR x = ci
           RETURN c.short AS commit, c.subject AS subject, c.ts AS ts, a.name AS author,
                  sum(e.lines) AS lines, pr.number AS pr, pr.title AS pr_title,
                  collect(DISTINCT coalesce(i.number, ci.number)) AS issues,
                  collect(DISTINCT n.version + ' | ' + n.text) AS notes
           ORDER BY lines DESC LIMIT $limit""",
        {"fs": funcs, "limit": limit},
    )
    for c in changes:
        cid = "commit:" + c["commit"]
        t.node(cid, "Commit", f"{c['commit']} {c['subject'][:40]}", author=c["author"])
        t.edge(cid, sym["key"], "CHANGED")
        if c["pr"] is not None:
            t.node(f"pr:{c['pr']}", "PR", f"PR #{c['pr']}", title=c["pr_title"])
            t.edge(cid, f"pr:{c['pr']}", "PART_OF")
        for i in c["issues"]:
            if i is not None:
                t.node(f"issue:{i}", "Issue", f"#{i}")
                t.edge(f"pr:{c['pr']}" if c["pr"] is not None else cid, f"issue:{i}", "FIXES")
        for n in c["notes"]:
            if n:
                v = n.split(" | ")[0]
                t.node(f"release:{v}", "Release", v)
                t.edge(f"release:{v}", f"pr:{c['pr']}" if c["pr"] is not None else cid, "NOTES")
        c["ts"] = time.strftime("%Y-%m-%d", time.gmtime(c["ts"]))
        c["issues"] = [i for i in c["issues"] if i is not None]
        c["notes"] = [n for n in c["notes"] if n]
    mentions = t.q(
        """MATCH (x)<-[:MENTIONS]-(n:ChangeNote)
           WHERE (x:Function AND x.qname IN $fs) OR (x:Class AND x.qname = $key)
              OR (x:Class AND x.qname = $cls)
           RETURN DISTINCT n.version AS version, n.date AS date, n.text AS note ORDER BY date DESC LIMIT 10""",
        {"fs": funcs, "key": sym["key"], "cls": sym["key"].rsplit(".", 1)[0]},
    )
    for m in mentions:
        t.node(f"release:{m['version']}", "Release", m["version"])
        t.edge(f"release:{m['version']}", sym["key"], "MENTIONS")
    return t.done("why", {"symbol": symbol}, {"target": sym, "changes": changes, "changelog_mentions": mentions})


# -------------------------------------------------------------- structure
def connection(repo: str, a: str, b: str) -> dict:
    """How are two pieces of code connected? Shortest path over calls/structure."""
    t = Trace(repo)
    sa, sb = resolve(t, a), resolve(t, b)
    if not sa or not sb:
        return t.done("connection", {"a": a, "b": b}, {"error": "could not resolve both symbols"})
    key = lambda s: "qname" if s["label"] != "Module" else "path"
    best = None
    for src, dst, direction in ((sa, sb, "a calls into b"), (sb, sa, "b calls into a")):
        r = t.q(
            f"""MATCH (x:{src['label']} {{{key(src)}: $a}}), (y:{dst['label']} {{{key(dst)}: $b}})
                RETURN [n IN nodes(shortestPath((x)-[:CALLS|HAS_METHOD|INSTANTIATES|DEFINES|IMPORTS|INHERITS*1..10]->(y))) |
                        coalesce(n.qname, n.path)] AS path,
                       [e IN relationships(shortestPath((x)-[:CALLS|HAS_METHOD|INSTANTIATES|DEFINES|IMPORTS|INHERITS*1..10]->(y))) | type(e)] AS rels""",
            {"a": src["key"], "b": dst["key"]},
        )
        if r and r[0]["path"] and (best is None or len(r[0]["path"]) < len(best[0])):
            best = (r[0]["path"], r[0]["rels"], direction)
    if not best:
        # Fall back to shared dependencies: what do they both reach?
        shared = t.q(
            """MATCH (x:Function)-[:CALLS*1..3]->(s:Function)<-[:CALLS*1..3]-(y:Function)
               WHERE x.qname = $a AND y.qname = $b AND NOT s.is_test
               RETURN DISTINCT s.qname AS shared LIMIT 10""", {"a": sa["key"], "b": sb["key"]})
        return t.done("connection", {"a": a, "b": b}, {"path": None, "shared_dependencies": [s["shared"] for s in shared]})
    path, rels, direction = best
    for n in path:
        t.node(n, "Function" if n.count(".") > 1 and "/" not in n else "Module", short(n))
    for x, y, rel in zip(path, path[1:], rels):
        t.edge(x, y, rel)
    return t.done("connection", {"a": a, "b": b}, {"direction": direction, "path": path, "relationships": rels, "hops": len(rels)})


def hotspots(repo: str, limit: int = 10) -> dict:
    """Critical + volatile code: PageRank over the call graph crossed with git churn."""
    t = Trace(repo)
    pr = t.q(
        """CALL algo.pageRank('Function', 'CALLS') YIELD node, score
           WHERE NOT node.is_test AND NOT (node.path STARTS WITH 'tests/' OR node.path STARTS WITH 'test/' OR node.path STARTS WITH 'examples/' OR node.path STARTS WITH 'docs/')
           RETURN node.qname AS qname, score ORDER BY score DESC LIMIT 60""")
    names = [r["qname"] for r in pr]
    churn = {r["qname"]: r for r in t.q(
        """MATCH (f:Function) WHERE f.qname IN $q
           OPTIONAL MATCH (f)<-[:CHANGED]-(c:Commit)
           OPTIONAL MATCH (f)<-[:WROTE]-(p:Person)
           OPTIONAL MATCH (f)<-[:CALLS]-(caller:Function)
           RETURN f.qname AS qname, count(DISTINCT c) AS commits, count(DISTINCT p) AS authors,
                  count(DISTINCT caller) AS fan_in, f.loc AS loc""", {"q": names})}
    top = max((r["score"] for r in pr), default=1) or 1
    out = []
    for r in pr:
        c = churn.get(r["qname"], {})
        rank = r["score"] / top
        risk = rank * (1 + c.get("commits", 0)) * (1 + c.get("authors", 0) / 4)
        out.append({"qname": r["qname"], "pagerank": round(r["score"], 5), "commits": c.get("commits", 0),
                    "authors": c.get("authors", 0), "fan_in": c.get("fan_in", 0), "loc": c.get("loc"),
                    "risk_score": round(risk, 3)})
    out.sort(key=lambda r: -r["risk_score"])
    for r in out[:limit]:
        t.node(r["qname"], "Function", short(r["qname"]), role="hotspot", score=r["risk_score"])
    return t.done("hotspots", {"limit": limit}, out[:limit])


def bridges(repo: str, limit: int = 10) -> dict:
    """Chokepoints: functions most call chains pass through (betweenness centrality)."""
    t = Trace(repo)
    res = t.q(
        """CALL algo.betweenness({nodeLabels: ['Function'], relationshipTypes: ['CALLS']})
           YIELD node, score WHERE NOT node.is_test AND score > 0
           RETURN node.qname AS qname, node.path AS path, score ORDER BY score DESC LIMIT $limit""",
        {"limit": limit})
    for r in res:
        t.node(r["qname"], "Function", short(r["qname"]), role="hotspot", score=r["score"])
    return t.done("bridges", {"limit": limit}, res)


def components(repo: str) -> dict:
    """Subsystems: community detection (label propagation) over the module import graph."""
    t = Trace(repo)
    res = t.q(
        """CALL algo.labelPropagation({nodeLabels: ['Module'], relationshipTypes: ['IMPORTS']})
           YIELD node, communityId WHERE node.exists = true AND NOT node.is_test
           RETURN communityId AS community, collect(node.path) AS modules, count(*) AS size
           ORDER BY size DESC LIMIT 12""")
    return t.done("components", {}, res)


def recent_activity(repo: str, days: int = 120, limit: int = 15) -> dict:
    t = Trace(repo)
    head = t.q("MATCH (c:Commit) RETURN max(c.ts) AS ts")[0]["ts"]
    res = t.q(
        """MATCH (p:Person)-[:AUTHORED]->(c:Commit)-[:MODIFIED]->(m:Module)
           WHERE c.ts > $since AND NOT c.is_merge
           OPTIONAL MATCH (c)-[:PART_OF]->(pr:PR)
           RETURN m.path AS module, count(DISTINCT c) AS commits, collect(DISTINCT p.name)[0..4] AS people,
                  collect(DISTINCT pr.number)[0..5] AS prs ORDER BY commits DESC LIMIT $limit""",
        {"since": head - days * 86400, "limit": limit})
    return t.done("recent_activity", {"days": days}, res)


def person_profile(repo: str, person: str) -> dict:
    t = Trace(repo)
    p = t.q("MATCH (p:Person) WHERE toLower(p.name) CONTAINS toLower($n) "
            "OPTIONAL MATCH (p)-[:AUTHORED]->(c:Commit) RETURN p.name AS name, count(c) AS commits "
            "ORDER BY commits DESC LIMIT 1", {"n": person})
    if not p:
        return t.done("person_profile", {"person": person}, {"error": f"no person matching '{person}'"})
    name = p[0]["name"]
    owns = t.q(
        """MATCH (:Person {name: $n})-[w:WROTE]->(f:Function) WHERE NOT f.is_test
           RETURN f.qname AS qname, w.lines AS lines, f.loc AS loc ORDER BY lines DESC LIMIT 15""", {"n": name})
    mods = t.q(
        """MATCH (:Person {name: $n})-[:AUTHORED]->(c:Commit)-[:MODIFIED]->(m:Module)
           RETURN m.path AS module, count(c) AS commits, max(c.ts) AS last ORDER BY commits DESC LIMIT 10""", {"n": name})
    for m in mods:
        m["last"] = time.strftime("%Y-%m-%d", time.gmtime(m["last"]))
    t.node("person:" + name, "Person", name)
    for o in owns[:10]:
        t.node(o["qname"], "Function", short(o["qname"]))
        t.edge("person:" + name, o["qname"], "WROTE")
    return t.done("person_profile", {"person": person}, {"name": name, "commits": p[0]["commits"],
                                                        "owns_most_of": owns, "modules": mods})


# ---------------------------------------------------------------- reviews
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _diff_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    files: dict[str, list[tuple[int, int]]] = defaultdict(list)
    cur = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            cur = p[2:] if p.startswith("b/") else p
            if cur == "/dev/null":
                cur = None
        elif cur and (m := HUNK_RE.match(line)):
            start, n = int(m.group(1)), int(m.group(2) or 1)
            files[cur].append((start, start + max(n, 1) - 1))
    return files


def review_change(repo: str, diff: str | None = None, symbols: list[str] | None = None) -> dict:
    """Act on a proposed change: blast radius, reviewers, tests to run, and past decisions."""
    t = Trace(repo)
    changed: list[str] = []
    if diff:
        for path, ranges in _diff_ranges(diff).items():
            for lo, hi in ranges:
                hit = t.q(
                    """MATCH (f:Function {path: $p}) WHERE f.line <= $hi AND f.end_line >= $lo
                       RETURN f.qname AS q ORDER BY f.end_line - f.line LIMIT 1""",
                    {"p": path, "lo": lo, "hi": hi})
                changed += [h["q"] for h in hit]
    for s in symbols or []:
        r = resolve(t, s)
        if r and r["label"] == "Function":
            changed.append(r["key"])
    changed = list(dict.fromkeys(changed))
    if not changed:
        return t.done("review_change", {}, {"error": "the change did not map to any indexed function"})
    per = []
    all_callers, all_tests, people = set(), set(), defaultdict(float)
    sub_cypher = []
    for q in changed[:8]:
        imp = impact(repo, q, depth=3)
        ex = experts(repo, q, limit=3)
        ws = why(repo, q, limit=3)
        sub_cypher += imp["cypher"][:3] + ex["cypher"][:2] + ws["cypher"][:1]
        r = imp["result"]
        all_callers |= {c["qname"] for c in r.get("callers", [])}
        all_tests |= set(r.get("tests_exercising", []))
        for e in ex["result"].get("experts", []):
            people[e["person"]] += e["score"]
        for n in imp["evidence"]["nodes"] + ex["evidence"]["nodes"] + ws["evidence"]["nodes"]:
            t.nodes.setdefault(n["id"], n)
        for e in imp["evidence"]["edges"] + ex["evidence"]["edges"] + ws["evidence"]["edges"]:
            t.edge(e["source"], e["target"], e["type"])
        per.append({"function": q, "risk": r.get("risk"), "affected_functions": r.get("affected_functions"),
                    "tests": r.get("test_count"),
                    "last_decisions": [{"pr": c["pr"], "title": c["pr_title"] or c["subject"], "notes": c["notes"][:1]}
                                       for c in ws["result"].get("changes", [])[:2]]})
    for q in changed:
        t.nodes.setdefault(q, {"id": q, "label": "Function", "name": short(q)})
        t.nodes[q]["role"] = "changed"
    t.cypher += sub_cypher
    order = {"high": 3, "medium": 2, "low": 1, None: 0}
    overall = max((p["risk"] for p in per), key=lambda r: order[r], default="low")
    untested = [p["function"] for p in per if not p["tests"]]
    reviewers = [p for p, _ in sorted(people.items(), key=lambda kv: -kv[1]) if not BOT_RE.search(p)][:3]
    files = sorted({c.split(".")[0] for c in all_tests})
    plan = {
        "changed_functions": changed, "overall_risk": overall, "per_function": per,
        "suggested_reviewers": reviewers, "affected_functions": len(all_callers),
        "tests_to_run": sorted(all_tests)[:40], "untested_changes": untested,
    }
    plan["markdown"] = _review_markdown(plan)
    return t.done("review_change", {"symbols": symbols, "diff_lines": len((diff or "").splitlines())}, plan)


def _review_markdown(p: dict) -> str:
    lines = [f"## Tracewise review plan — risk: **{p['overall_risk']}**", ""]
    lines.append(f"Changed: {', '.join('`' + short(c) + '`' for c in p['changed_functions'])}")
    lines.append(f"Blast radius: {p['affected_functions']} functions call into this change.")
    if p["suggested_reviewers"]:
        lines.append(f"Suggested reviewers: {', '.join(p['suggested_reviewers'])}")
    if p["untested_changes"]:
        lines.append(f"⚠️ No test reaches: {', '.join('`' + short(u) + '`' for u in p['untested_changes'])}")
    lines += ["", "| Function | Risk | Callers | Tests | Last decision |", "|---|---|---|---|---|"]
    for f in p["per_function"]:
        d = f["last_decisions"][0] if f["last_decisions"] else None
        dec = f"PR #{d['pr']}: {d['title']}" if d and d["pr"] else (d["title"] if d else "—")
        lines.append(f"| `{short(f['function'])}` | {f['risk']} | {f['affected_functions']} | {f['tests']} | {dec[:70]} |")
    if p["tests_to_run"]:
        lines += ["", "Run first:", "```", "pytest " + " ".join(
            t.rsplit(".", 1)[0].replace(".", "/") + ".py::" + t.rsplit(".", 1)[1] for t in p["tests_to_run"][:12]), "```"]
    return "\n".join(lines)


_WRITE_RE = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD|FOREACH)\b", re.I)
_STRIP_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`[^`]*`|//[^\n]*|/\*.*?\*/", re.S)


def run_cypher(repo: str, query: str, timeout_ms: int = 5000) -> dict:
    """Read-only escape hatch for questions no other tool covers.

    Two layers: a fast pre-check on the query with strings and comments stripped (so a
    name like 'DELETE' doesn't trip it), then FalkorDB's own GRAPH.RO_QUERY, which
    refuses any write, write procedure or multi-statement query at the engine level.
    A timeout bounds expensive traversals.
    """
    t = Trace(repo)
    code = _STRIP_RE.sub("''", query)
    if ";" in code.rstrip().rstrip(";") or _WRITE_RE.search(code) or re.search(r"\bCALL\s*\{", code, re.I):
        return t.done("run_cypher", {"query": query}, {"error": "only single read-only queries are allowed"})
    if not re.search(r"\bLIMIT\b", code, re.I) and re.search(r"\bRETURN\b", code, re.I):
        query = query.rstrip().rstrip(";") + " LIMIT 50"
    t.cypher.append(" ".join(query.split()))
    try:
        res = rows(t.g.ro_query(query, timeout=timeout_ms))  # engine-enforced read-only
    except Exception as exc:
        return t.done("run_cypher", {"query": query}, {"error": str(exc)[:300]})
    return t.done("run_cypher", {"query": query}, res[:50])


TOOLS = {
    "find_symbol": find_symbol, "graph_schema": graph_schema, "impact": impact, "tests_for": tests_for,
    "experts": experts, "why": why, "connection": connection, "hotspots": hotspots, "bridges": bridges,
    "components": components, "recent_activity": recent_activity, "person_profile": person_profile,
    "review_change": review_change, "run_cypher": run_cypher,
}
