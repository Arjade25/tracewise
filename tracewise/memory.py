"""Agent memory in FalkorDB: one isolated graph per user (``memory:<user>``).

Episodic   (:Session)-[:HAS_TURN]->(:Turn)-[:NEXT]->(:Turn)
           (:Turn)-[:USED]->(:ToolCall)-[:TOUCHED]->(:Entity)
Semantic   (:Fact {kind, text, value})-[:ABOUT]->(:Entity {key, repo})
           (:Fact)-[:SUPERSEDES]->(:Fact)            # knowledge updates, history kept
Procedural (:Procedure {intent})-[:STEP {order}]->(:Tool)  with success counters
Shared     (:Task)-[:ASSIGNED_TO]->(:Agent), (:Task)-[:PRODUCED]->(:Fact),
           (:Agent)-[:HANDOFF {note}]->(:Agent)       # multi-agent coordination

``Entity.key`` is the node id in the repo graph (a qname, a module path,
``person:<name>``), so memory and code knowledge join on the same identifiers.
"""
from __future__ import annotations

import json
import time
import uuid

from .db import memory_graph, rows


def _now() -> int:
    return int(time.time() * 1000)


class Memory:
    def __init__(self, user: str = "default"):
        self.user = user
        self.g = memory_graph(user)
        for label, prop in [("Session", "id"), ("Turn", "id"), ("Entity", "key"), ("Fact", "id"),
                            ("Task", "id"), ("Agent", "name"), ("Procedure", "intent"), ("Tool", "name")]:
            try:
                self.g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception:
                pass

    def q(self, query: str, params: dict | None = None) -> list[dict]:
        return rows(self.g.query(query, params or {}))

    # ------------------------------------------------------------ episodic
    def start_session(self, repo: str, session_id: str | None = None) -> str:
        sid = session_id or uuid.uuid4().hex[:12]
        self.q("""MERGE (s:Session {id: $id})
                  ON CREATE SET s.started = $ts, s.repo = $repo, s.user = $user
                  SET s.last_active = $ts""",
               {"id": sid, "ts": _now(), "repo": repo, "user": self.user})
        return sid

    def record_turn(self, session: str, question: str, answer: str, tool_calls: list[dict],
                    intent: str | None = None, mode: str | None = None) -> str:
        tid = uuid.uuid4().hex[:12]
        # Keep a compact copy of what the UI showed, so a past conversation can be reopened as it was.
        nodes, edges = {}, set()
        for c in tool_calls:
            for n in c.get("evidence", {}).get("nodes", []):
                nodes.setdefault(n["id"], n)
            for e in c.get("evidence", {}).get("edges", []):
                edges.add((e["source"], e["target"], e["type"]))
        evidence = {"nodes": list(nodes.values())[:200],
                    "edges": [{"source": a, "target": b, "type": t} for a, b, t in list(edges)[:400]]}
        tools = [{"tool": c["tool"], "ms": c.get("ms"), "cypher": c.get("cypher", [])[:20],
                  "learned": c.get("learned", False)} for c in tool_calls]
        self.q("""MATCH (s:Session {id: $sid})
                  OPTIONAL MATCH (s)-[:HAS_TURN]->(last:Turn) WHERE NOT (last)-[:NEXT]->(:Turn)
                  CREATE (t:Turn {id: $tid, ts: $ts, question: $q, answer: $a, intent: $intent, mode: $mode,
                                  evidence: $ev, tools: $tools})
                  CREATE (s)-[:HAS_TURN]->(t)
                  SET s.last_active = $ts, s.title = coalesce(s.title, left($q, 80)),
                      s.turns = coalesce(s.turns, 0) + 1
                  FOREACH (_ IN CASE WHEN last IS NULL THEN [] ELSE [1] END | CREATE (last)-[:NEXT]->(t))""",
               {"sid": session, "tid": tid, "ts": _now(), "q": question[:2000], "a": answer[:12000],
                "intent": intent, "mode": mode, "ev": json.dumps(evidence, default=str)[:200000],
                "tools": json.dumps(tools, default=str)[:60000]})
        repo = self.q("MATCH (s:Session {id: $sid}) RETURN s.repo AS r", {"sid": session})[0]["r"]
        for i, call in enumerate(tool_calls):
            ents = [n for n in call.get("evidence", {}).get("nodes", []) if n.get("role") in
                    ("target", "changed", "hotspot") or n["label"] == "Person"][:12]
            self.q("""MATCH (t:Turn {id: $tid})
                      CREATE (c:ToolCall {tool: $tool, args: $args, order: $i, ms: $ms})
                      CREATE (t)-[:USED]->(c)
                      WITH c
                      UNWIND $ents AS e
                      MERGE (x:Entity {key: e.id, repo: $repo})
                      ON CREATE SET x.label = e.label, x.name = e.name
                      MERGE (c)-[:TOUCHED]->(x)""",
                   {"tid": tid, "tool": call["tool"], "args": json.dumps(call.get("args", {}))[:500], "i": i,
                    "ms": call.get("ms", 0), "repo": repo,
                    "ents": [{"id": e["id"], "label": e["label"], "name": e["name"]} for e in ents]})
        ok = [c for c in tool_calls if not (isinstance(c.get("result"), dict) and "error" in c["result"])]
        if intent and ok and len(ok) == len(tool_calls):  # learn only from turns where every tool succeeded
            self.learn_procedure(intent, [c["tool"] for c in tool_calls if c["tool"] != "recall"])
        return tid

    # ------------------------------------------------------- conversations
    def list_sessions(self, repo: str | None = None, limit: int = 50) -> list[dict]:
        """Past conversations (sessions with at least one turn), newest first."""
        return self.q("""MATCH (s:Session) WHERE ($repo IS NULL OR s.repo = $repo) AND s.turns > 0
                         RETURN s.id AS id, s.repo AS repo, s.title AS title, s.turns AS turns,
                                s.started AS started, s.last_active AS last_active
                         ORDER BY last_active DESC LIMIT $n""", {"repo": repo, "n": limit})

    def session_turns(self, session: str) -> dict | None:
        head = self.q("""MATCH (s:Session {id: $sid}) RETURN s.id AS id, s.repo AS repo, s.title AS title,
                                s.started AS started""", {"sid": session})
        if not head:
            return None
        turns = self.q("""MATCH (:Session {id: $sid})-[:HAS_TURN]->(t:Turn)
                          RETURN t.id AS id, t.ts AS ts, t.question AS question, t.answer AS answer,
                                 t.intent AS intent, t.mode AS mode, t.evidence AS evidence, t.tools AS tools
                          ORDER BY ts""", {"sid": session})
        for t in turns:
            t["evidence"] = json.loads(t["evidence"]) if t.get("evidence") else {"nodes": [], "edges": []}
            t["tools"] = json.loads(t["tools"]) if t.get("tools") else []
        return {**head[0], "turns": turns}

    def delete_session(self, session: str) -> int:
        """Remove a conversation (its turns and tool calls). Learned facts and procedures are kept."""
        r = self.q("""MATCH (s:Session {id: $sid})
                      OPTIONAL MATCH (s)-[:HAS_TURN]->(t:Turn)
                      OPTIONAL MATCH (t)-[:USED]->(c:ToolCall)
                      WITH s, collect(DISTINCT t) AS ts, collect(DISTINCT c) AS cs
                      FOREACH (x IN cs | DETACH DELETE x)
                      FOREACH (x IN ts | DETACH DELETE x)
                      DETACH DELETE s
                      RETURN size(ts) AS n""", {"sid": session})
        return r[0]["n"] if r else 0

    def recent_turns(self, session: str, limit: int = 6) -> list[dict]:
        return self.q("""MATCH (:Session {id: $sid})-[:HAS_TURN]->(t:Turn)
                         RETURN t.question AS question, t.answer AS answer, t.ts AS ts
                         ORDER BY t.ts DESC LIMIT $n""", {"sid": session, "n": limit})[::-1]

    def history_about(self, keys: list[str], exclude_session: str | None = None, limit: int = 5) -> list[dict]:
        """Earlier turns, in any session, that touched the same code or people."""
        return self.q("""MATCH (s:Session)-[:HAS_TURN]->(t:Turn)-[:USED]->(:ToolCall)-[:TOUCHED]->(e:Entity)
                         WHERE e.key IN $keys AND s.id <> $ex
                         RETURN DISTINCT t.question AS question, left(t.answer, 400) AS answer, t.ts AS ts,
                                s.id AS session, collect(DISTINCT e.name) AS about
                         ORDER BY ts DESC LIMIT $n""",
                      {"keys": keys, "ex": exclude_session or "", "n": limit})

    # ------------------------------------------------------------ semantic
    def remember(self, repo: str, key: str, kind: str, text: str, value=None, source: str = "agent",
                 task: str | None = None, label: str | None = None, name: str | None = None) -> dict:
        """Store a fact about an entity. A newer fact of the same kind supersedes the old one."""
        fid = uuid.uuid4().hex[:12]
        val = json.dumps(value) if value is not None else None
        res = self.q("""MERGE (e:Entity {key: $key, repo: $repo})
                        ON CREATE SET e.label = $label, e.name = $name
                        WITH e
                        OPTIONAL MATCH (old:Fact {kind: $kind, current: true})-[:ABOUT]->(e)
                        CREATE (f:Fact {id: $fid, kind: $kind, text: $text, value: $val, ts: $ts,
                                        source: $source, current: true})
                        CREATE (f)-[:ABOUT]->(e)
                        FOREACH (_ IN CASE WHEN old IS NULL THEN [] ELSE [1] END |
                            SET old.current = false CREATE (f)-[:SUPERSEDES]->(old))
                        RETURN old.text AS previous, old.value AS previous_value""",
                     {"key": key, "repo": repo, "kind": kind, "text": text, "val": val, "ts": _now(),
                      "source": source, "fid": fid, "label": label or "Entity", "name": name or key})
        if task:
            self.q("""MATCH (t:Task {id: $task}), (f:Fact {id: $fid}) MERGE (t)-[:PRODUCED]->(f)""",
                   {"task": task, "fid": fid})
        prev = res[0] if res else {}
        changed = prev.get("previous_value") is not None and prev.get("previous_value") != val
        return {"id": fid, "previous": prev.get("previous"), "changed": changed}

    def facts_about(self, keys: list[str], include_history: bool = False) -> list[dict]:
        cond = "" if include_history else "AND f.current = true"
        return self.q(f"""MATCH (f:Fact)-[:ABOUT]->(e:Entity) WHERE e.key IN $keys {cond}
                          OPTIONAL MATCH (f)-[:SUPERSEDES]->(old:Fact)
                          RETURN e.key AS key, f.kind AS kind, f.text AS text, f.ts AS ts, f.source AS source,
                                 old.text AS replaced
                          ORDER BY ts DESC LIMIT 40""", {"keys": keys})

    def fact_timeline(self, key: str) -> list[dict]:
        return self.q("""MATCH (f:Fact)-[:ABOUT]->(:Entity {key: $key})
                         RETURN f.kind AS kind, f.text AS text, f.ts AS ts, f.current AS current, f.source AS source
                         ORDER BY ts""", {"key": key})

    def forget(self, key: str) -> int:
        return self.q("""MATCH (f:Fact)-[:ABOUT]->(:Entity {key: $key}) WITH collect(f) AS fs
                         FOREACH (x IN fs | DETACH DELETE x) RETURN size(fs) AS n""", {"key": key})[0]["n"]

    # ---------------------------------------------------------- procedural
    def learn_procedure(self, intent: str, tools: list[str]) -> None:
        tools = list(dict.fromkeys(tools))[:8]
        self.q("""MERGE (p:Procedure {intent: $intent})
                  ON CREATE SET p.uses = 0
                  SET p.uses = p.uses + 1, p.last_used = $ts, p.sequence = $seq
                  WITH p
                  OPTIONAL MATCH (p)-[old:STEP]->() DELETE old
                  WITH DISTINCT p
                  UNWIND range(0, size($seq) - 1) AS i
                  MERGE (t:Tool {name: $seq[i]})
                  CREATE (p)-[:STEP {order: i}]->(t)""",
               {"intent": intent, "seq": tools, "ts": _now()})

    def procedure_for(self, intent: str) -> list[str] | None:
        r = self.q("""MATCH (p:Procedure {intent: $intent})-[s:STEP]->(t:Tool)
                      RETURN t.name AS tool ORDER BY s.order""", {"intent": intent})
        return [x["tool"] for x in r] or None

    def procedure_info(self, intent: str) -> dict | None:
        r = self.q("""MATCH (p:Procedure {intent: $intent}) RETURN p.uses AS uses, p.sequence AS steps""",
                   {"intent": intent})
        return {"intent": intent, **r[0]} if r and r[0]["steps"] else None

    # ------------------------------------------------------- coordination
    def create_task(self, goal: str, agent: str, parent: str | None = None, payload: dict | None = None) -> str:
        tid = uuid.uuid4().hex[:10]
        self.q("""MERGE (a:Agent {name: $agent})
                  CREATE (t:Task {id: $tid, goal: $goal, status: 'open', created: $ts, payload: $payload})
                  CREATE (t)-[:ASSIGNED_TO]->(a)
                  WITH t
                  OPTIONAL MATCH (p:Task {id: $parent})
                  FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END | CREATE (p)-[:SUBTASK]->(t))""",
               {"tid": tid, "goal": goal, "agent": agent, "ts": _now(), "parent": parent,
                "payload": json.dumps(payload or {})})
        return tid

    def claim(self, agent: str) -> dict | None:
        r = self.q("""MATCH (t:Task {status: 'open'})-[:ASSIGNED_TO]->(:Agent {name: $agent})
                      WITH t ORDER BY t.created LIMIT 1
                      SET t.status = 'running', t.started = $ts
                      RETURN t.id AS id, t.goal AS goal, t.payload AS payload""",
                   {"agent": agent, "ts": _now()})
        if not r:
            return None
        r[0]["payload"] = json.loads(r[0]["payload"] or "{}")
        return r[0]

    def complete(self, task: str, summary: str, result: dict | None = None) -> None:
        self.q("""MATCH (t:Task {id: $id}) SET t.status = 'done', t.finished = $ts, t.summary = $s,
                  t.result = $r""", {"id": task, "ts": _now(), "s": summary[:2000],
                                     "r": json.dumps(result or {}, default=str)[:20000]})

    def handoff(self, src: str, dst: str, task: str, note: str) -> None:
        self.q("""MERGE (a:Agent {name: $src}) MERGE (b:Agent {name: $dst})
                  CREATE (a)-[:HANDOFF {task: $task, note: $note, ts: $ts}]->(b)""",
               {"src": src, "dst": dst, "task": task, "note": note[:500], "ts": _now()})

    def task_tree(self, root: str) -> dict:
        root_row = self.q("""MATCH (t:Task {id: $id})-[:ASSIGNED_TO]->(a:Agent)
                             RETURN t.id AS id, t.goal AS goal, t.status AS status, a.name AS agent,
                                    t.summary AS summary""", {"id": root})
        subs = self.q("""MATCH (:Task {id: $id})-[:SUBTASK]->(t:Task)-[:ASSIGNED_TO]->(a:Agent)
                         OPTIONAL MATCH (t)-[:PRODUCED]->(f:Fact)
                         RETURN t.id AS id, t.goal AS goal, t.status AS status, a.name AS agent,
                                t.summary AS summary, t.started AS started, t.finished AS finished,
                                count(f) AS facts, t.created AS created ORDER BY created""", {"id": root})
        hand = self.q("""MATCH (a:Agent)-[h:HANDOFF]->(b:Agent) WHERE h.task = $id OR h.task IN $subs
                         RETURN a.name AS source, b.name AS target, h.note AS note ORDER BY h.ts""",
                      {"id": root, "subs": [s["id"] for s in subs]})
        return {"task": root_row[0] if root_row else None, "subtasks": subs, "handoffs": hand}

    def stats(self) -> dict:
        r = self.q("""MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n""")
        return {x["label"]: x["n"] for x in r}
