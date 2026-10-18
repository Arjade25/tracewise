# Cypher and graph algorithms Tracewise depends on

Every answer the agent gives comes from one or more of these queries. The UI shows
the exact queries behind each answer (the **Cypher** drawer), and every tool result
over HTTP or MCP includes them in a `cypher` field.

Parameters are shown as `$name`. Source: [`tracewise/tools.py`](../tracewise/tools.py),
[`tracewise/memory.py`](../tracewise/memory.py), [`tracewise/crew.py`](../tracewise/crew.py).

## 1. Blast radius (`impact`)

Overridden base-class methods count as targets too, because calls resolved against the
base method still dispatch to the override at runtime:

```cypher
MATCH (c:Class)-[:HAS_METHOD]->(f:Function {qname: $q})
MATCH (c)-[:INHERITS*1..4]->(:Class)-[:HAS_METHOD]->(o:Function {name: f.name})
RETURN o.qname AS q
```

Transitive callers, with distance (multi-hop traversal):

```cypher
MATCH (tgt:Function) WHERE tgt.qname IN $targets
MATCH p = (c:Function)-[:CALLS*1..3]->(tgt)
WHERE NOT c.qname IN $targets
WITH c, min(length(p)) AS dist
RETURN c.qname AS qname, c.path AS path, c.is_test AS test, dist
ORDER BY dist, qname
```

The call chain drawn in the UI is a shortest path per caller:

```cypher
MATCH (tgt:Function) WHERE tgt.qname IN $targets
MATCH (c:Function) WHERE c.qname IN $callers
WITH c, tgt, shortestPath((c)-[:CALLS*1..5]->(tgt)) AS p WHERE p IS NOT NULL
RETURN [n IN nodes(p) | n.qname] AS chain
```

## 2. Tests that execute a function (`tests_for`)

Measured coverage first (edges written by running the target repo's test suite):

```cypher
MATCH (tst:Function)-[e:COVERS]->(tgt:Function) WHERE tgt.qname IN $targets
RETURN tst.qname AS test, tst.path AS path, sum(e.lines) AS lines
```

…then static call paths from test functions as a fallback:

```cypher
MATCH (tgt:Function) WHERE tgt.qname IN $targets
MATCH p = (tst:Function {is_test: true})-[:CALLS*1..4]->(tgt)
WITH tst, min(length(p)) AS hops
RETURN tst.qname AS test, tst.path AS path, hops
```

## 3. Who knows this code (`experts`)

Four signals, scored and combined, each kept as evidence:

```cypher
// surviving lines per author (git blame at HEAD)
MATCH (p:Person)-[w:WROTE]->(f:Function) WHERE f.qname IN $fs
RETURN p.name AS person, sum(w.lines) AS lines

// authorship of directly connected code
MATCH (f:Function)-[:CALLS]-(n:Function)<-[w:WROTE]-(p:Person)
WHERE f.qname IN $fs AND NOT n.qname IN $fs AND NOT n.is_test
RETURN p.name AS person, count(DISTINCT n) AS fns, sum(w.lines) AS lines

// recent work on the module
MATCH (m:Module {path: $path})<-[:MODIFIED]-(c:Commit)<-[:AUTHORED]-(p:Person)
WHERE c.ts > $since AND NOT c.is_merge
RETURN p.name AS person, count(c) AS commits, max(c.ts) AS last

// who merged the PRs that changed it
MATCH (f:Function)<-[:CHANGED]-(c:Commit)-[:PART_OF]->(pr:PR)<-[:PART_OF]-(m:Commit)<-[:AUTHORED]-(p:Person)
WHERE f.qname IN $fs AND m.sha = pr.merge_sha
RETURN p.name AS person, count(DISTINCT pr) AS prs
```

## 4. Decision trace (`why`)

Function → commit → PR → issue → release note, in one query:

```cypher
MATCH (f:Function)<-[e:CHANGED]-(c:Commit)<-[:AUTHORED]-(a:Person) WHERE f.qname IN $fs
OPTIONAL MATCH (c)-[:PART_OF]->(pr:PR)
OPTIONAL MATCH (pr)-[:FIXES]->(i:Issue)
OPTIONAL MATCH (c)-[:FIXES]->(ci:Issue)
OPTIONAL MATCH (n:ChangeNote)-[:REFERENCES]->(x) WHERE x = pr OR x = i OR x = ci
RETURN c.short AS commit, c.subject AS subject, c.ts AS ts, a.name AS author,
       sum(e.lines) AS lines, pr.number AS pr, pr.title AS pr_title,
       collect(DISTINCT coalesce(i.number, ci.number)) AS issues,
       collect(DISTINCT n.version + ' | ' + n.text) AS notes
ORDER BY lines DESC LIMIT $limit
```

Release notes that name the code directly:

```cypher
MATCH (x)<-[:MENTIONS]-(n:ChangeNote)
WHERE (x:Function AND x.qname IN $fs) OR (x:Class AND x.qname = $key) OR (x:Class AND x.qname = $cls)
RETURN DISTINCT n.version AS version, n.date AS date, n.text AS note ORDER BY date DESC LIMIT 10
```

## 5. How two things connect (`connection`)

```cypher
MATCH (x:Function {qname: $a}), (y:Function {qname: $b})
RETURN [n IN nodes(shortestPath((x)-[:CALLS|HAS_METHOD|INSTANTIATES|DEFINES|IMPORTS|INHERITS*1..10]->(y))) |
        coalesce(n.qname, n.path)] AS path
```

## 6. Graph algorithms used as agent tools

| Tool | Algorithm | Query |
|---|---|---|
| `hotspots` | PageRank over `CALLS`, crossed with churn and author count | `CALL algo.pageRank('Function', 'CALLS') YIELD node, score` |
| `bridges` | Betweenness centrality over `CALLS` | `CALL algo.betweenness({nodeLabels: ['Function'], relationshipTypes: ['CALLS']}) YIELD node, score` |
| `components` | Label propagation (community detection) over `IMPORTS` | `CALL algo.labelPropagation({nodeLabels: ['Module'], relationshipTypes: ['IMPORTS']}) YIELD node, communityId` |
| `connection`, `impact` | Shortest path | `shortestPath((a)-[...*]->(b))` |

## 7. Reviewing a diff (`review_change`)

Each diff hunk is mapped to the innermost function whose line range it overlaps:

```cypher
MATCH (f:Function {path: $p}) WHERE f.line <= $hi AND f.end_line >= $lo
RETURN f.qname AS q ORDER BY f.end_line - f.line LIMIT 1
```

…then `impact`, `experts` and `why` run for each changed function.

## 8. Memory (one graph per user: `memory:<user>`)

Store a fact; a newer fact of the same kind about the same entity supersedes the old one
(the old one is kept, marked `current: false`, linked by `SUPERSEDES`):

```cypher
MERGE (e:Entity {key: $key, repo: $repo})
WITH e
OPTIONAL MATCH (old:Fact {kind: $kind, current: true})-[:ABOUT]->(e)
CREATE (f:Fact {id: $fid, kind: $kind, text: $text, value: $val, ts: $ts, source: $source, current: true})
CREATE (f)-[:ABOUT]->(e)
FOREACH (_ IN CASE WHEN old IS NULL THEN [] ELSE [1] END |
    SET old.current = false CREATE (f)-[:SUPERSEDES]->(old))
RETURN old.text AS previous, old.value AS previous_value
```

Recall earlier turns, from any session, that touched the same code or people:

```cypher
MATCH (s:Session)-[:HAS_TURN]->(t:Turn)-[:USED]->(:ToolCall)-[:TOUCHED]->(e:Entity)
WHERE e.key IN $keys AND s.id <> $ex
RETURN DISTINCT t.question AS question, left(t.answer, 400) AS answer, t.ts AS ts,
       s.id AS session, collect(DISTINCT e.name) AS about
ORDER BY ts DESC LIMIT $n
```

Append a turn to the episode chain:

```cypher
MATCH (s:Session {id: $sid})
OPTIONAL MATCH (s)-[:HAS_TURN]->(last:Turn) WHERE NOT (last)-[:NEXT]->(:Turn)
CREATE (t:Turn {id: $tid, ts: $ts, question: $q, answer: $a, intent: $intent})
CREATE (s)-[:HAS_TURN]->(t)
FOREACH (_ IN CASE WHEN last IS NULL THEN [] ELSE [1] END | CREATE (last)-[:NEXT]->(t))
```

## 9. Multi-agent coordination (shared state in the memory graph)

Claim the next task. This is one query, and FalkorDB executes writes to a graph one at a
time, so concurrent workers never claim the same task:

```cypher
MATCH (t:Task {status: 'open'})-[:ASSIGNED_TO]->(:Agent {name: $agent})
WITH t ORDER BY t.created LIMIT 1
SET t.status = 'running', t.started = $ts
RETURN t.id AS id, t.goal AS goal, t.payload AS payload
```

The reviewer waits on the graph, then reads every analyst's findings back from it:

```cypher
MATCH (:Task {id: $id})-[:SUBTASK]->(t:Task {status: 'done'}) RETURN count(t) AS n

MATCH (:Task {id: $id})-[:SUBTASK]->(t:Task)-[:PRODUCED]->(f:Fact)-[:ABOUT]->(e:Entity)
MATCH (t)-[:ASSIGNED_TO]->(a:Agent)
RETURN e.key AS function, f.kind AS kind, f.text AS text, f.value AS value, a.name AS agent
```
