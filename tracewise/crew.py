"""A review crew: five roles (coordinator, three analysts, reviewer) that coordinate only through the shared memory graph.

    coordinator ──creates tasks──▶ impact-analyst ┐
                                   test-analyst   ├─ write Facts to the graph ─▶ reviewer
                                   history-analyst┘   and hand off               (reads Facts back)

Workers run concurrently. They never pass Python objects to each other: every task,
claim, finding and handoff is a node or edge in ``memory:<user>``. Claiming a task
is a single Cypher query, and FalkorDB runs writes to one graph serially, so two
workers can never claim the same task.
"""
from __future__ import annotations

import json
import threading
import time

from . import tools as T
from .memory import Memory

WORKERS = {
    "impact-analyst": "impact",
    "test-analyst": "tests_for",
    "history-analyst": "why",
}


def _summarise(tool: str, res: dict) -> tuple[str, dict]:
    r = res["result"]
    if "error" in r:
        return r["error"], {}
    if tool == "impact":
        return (f"{r['affected_functions']} callers in {len(r['affected_modules'])} modules, risk {r['risk']}",
                {"risk": r["risk"], "callers": r["affected_functions"],
                 "nearest": [c["qname"] for c in r["callers"][:5]]})
    if tool == "tests_for":
        return (f"{r['test_count']} tests reach it" if r["test_count"] else "UNTESTED",
                {"tests": r["test_count"], "run": [x["test"] for x in r["tests"][:8]]})
    c = r["changes"][0] if r.get("changes") else None
    if not c:
        return "no recorded history", {}
    return (f"last shaped by {'PR #' + str(c['pr']) if c['pr'] else c['commit']} ({c['author']}, {c['ts']})",
            {"pr": c["pr"], "title": c["pr_title"] or c["subject"], "author": c["author"],
             "notes": c["notes"][:1]})


def worker(user: str, repo: str, name: str, tool: str, stop: threading.Event, log: list) -> None:
    mem = Memory(user)
    while not stop.is_set():
        task = mem.claim(name)
        if not task:
            time.sleep(0.05)
            continue
        fn = task["payload"]["function"]
        log.append({"t": time.time(), "agent": name, "event": "claimed", "function": fn})
        res = getattr(T, tool)(repo, fn)
        text, value = _summarise(tool, res)
        mem.remember(repo, fn, f"crew:{tool}", text, value, source=name, task=task["id"],
                     label="Function", name=T.short(fn))
        if tool == "why" and value.get("author"):
            # The history analyst also records who to ask, for the reviewer to pick up.
            ex = T.experts(repo, fn, limit=3)["result"].get("experts", [])
            mem.remember(repo, fn, "crew:experts", ", ".join(e["person"] for e in ex),
                         [e["person"] for e in ex], source=name, task=task["id"])
        mem.complete(task["id"], text, value)
        mem.handoff(name, "reviewer", task["id"], f"{T.short(fn)}: {text}")
        log.append({"t": time.time(), "agent": name, "event": "done", "function": fn, "summary": text})


def review(repo: str, user: str, symbols: list[str] | None = None, diff: str | None = None,
           timeout: float = 30.0) -> dict:
    t0 = time.time()
    mem = Memory(user)
    # Coordinator: resolve the change to functions (diff hunks -> function line ranges).
    tr = T.Trace(repo)
    changed: list[str] = []
    if diff:
        for path, ranges in T._diff_ranges(diff).items():
            for lo, hi in ranges:
                hit = tr.q("""MATCH (f:Function {path: $p}) WHERE f.line <= $hi AND f.end_line >= $lo
                              RETURN f.qname AS q ORDER BY f.end_line - f.line LIMIT 1""",
                           {"p": path, "lo": lo, "hi": hi})
                changed += [h["q"] for h in hit]
    for s in symbols or []:
        r = T.resolve(tr, s)
        if r and r["label"] == "Function":
            changed.append(r["key"])
    changed = list(dict.fromkeys(changed))[:6]
    if not changed:
        return {"error": "the change did not map to any indexed function"}

    root = mem.create_task(f"Review change to {', '.join(T.short(c) for c in changed)}", "coordinator",
                           payload={"functions": changed})
    for fn in changed:
        for agent in WORKERS:
            mem.create_task(f"{agent}: {T.short(fn)}", agent, parent=root, payload={"function": fn})
    mem.handoff("coordinator", "impact-analyst", root, f"{len(changed)} functions to assess")
    mem.handoff("coordinator", "test-analyst", root, f"{len(changed)} functions to cover")
    mem.handoff("coordinator", "history-analyst", root, f"{len(changed)} functions to trace")

    stop, log = threading.Event(), []
    threads = [threading.Thread(target=worker, args=(user, repo, a, tool, stop, log), daemon=True)
               for a, tool in WORKERS.items()]
    for th in threads:
        th.start()

    # Reviewer: waits on the graph, not on the threads.
    expected = len(changed) * len(WORKERS)
    while time.time() - t0 < timeout:
        done = mem.q("""MATCH (:Task {id: $id})-[:SUBTASK]->(t:Task {status: 'done'}) RETURN count(t) AS n""",
                     {"id": root})[0]["n"]
        if done >= expected:
            break
        time.sleep(0.05)
    stop.set()
    for th in threads:
        th.join(timeout=2)

    findings = mem.q("""MATCH (:Task {id: $id})-[:SUBTASK]->(t:Task)-[:PRODUCED]->(f:Fact)-[:ABOUT]->(e:Entity)
                        MATCH (t)-[:ASSIGNED_TO]->(a:Agent)
                        RETURN e.key AS function, f.kind AS kind, f.text AS text, f.value AS value,
                               a.name AS agent""", {"id": root})
    per: dict[str, dict] = {fn: {} for fn in changed}
    for f in findings:
        per[f["function"]][f["kind"].split(":")[1]] = {"text": f["text"],
                                                       "value": json.loads(f["value"]) if f["value"] else None,
                                                       "agent": f["agent"]}
    order = {"high": 3, "medium": 2, "low": 1}
    risks = [(p.get("impact", {}).get("value") or {}).get("risk") for p in per.values()]
    overall = max((r for r in risks if r), key=lambda r: order[r], default="low")
    people: dict[str, int] = {}
    for p in per.values():
        for i, name in enumerate((p.get("experts", {}).get("value") or [])):
            if not T.BOT_RE.search(name):
                people[name] = people.get(name, 0) + 3 - i
    reviewers = [p for p, _ in sorted(people.items(), key=lambda kv: -kv[1])][:3]
    untested = [fn for fn, p in per.items() if (p.get("tests_for", {}).get("value") or {}).get("tests", 0) == 0]

    lines = [f"## Crew review — risk: **{overall}**", "",
             f"Reviewers: {', '.join(reviewers) or '—'}"]
    if untested:
        lines.append("⚠️ Untested: " + ", ".join(f"`{T.short(u)}`" for u in untested))
    lines += ["", "| Function | Impact (impact-analyst) | Tests (test-analyst) | History (history-analyst) |",
              "|---|---|---|---|"]
    for fn, p in per.items():
        lines.append(f"| `{T.short(fn)}` | {p.get('impact', {}).get('text', '—')} | "
                     f"{p.get('tests_for', {}).get('text', '—')} | {p.get('why', {}).get('text', '—')} |")
    summary = "\n".join(lines)
    mem.complete(root, summary, {"risk": overall, "reviewers": reviewers})
    mem.handoff("reviewer", "coordinator", root, f"review complete: risk {overall}")
    return {"task": root, "summary": summary, "risk": overall, "reviewers": reviewers, "untested": untested,
            "findings": per, "tree": mem.task_tree(root), "log": log, "ms": round((time.time() - t0) * 1000)}
