# Your codebase is a graph. Give your agent one.

*Draft for the Graph Hacks blog-post prize. Edit freely: it should sound like you.*

Every engineer has asked these questions, usually right before merging something scary:

1. What breaks if I change this?
2. Who should review it?
3. Why is it written like this in the first place?
4. Which tests should I run?

Coding agents are good at reading the file in front of them, but none of these questions live in one file. The answer to *what breaks* is in the call graph. *Who should review* is spread across years of `git blame`. *Why* is in a PR from 2021 that fixed an issue from 2019 and was summarised in a release note. An agent that only sees text has to rediscover all of that, every session, by grepping.

For Graph Hacks I built **Tracewise**: an agent whose context is a FalkorDB graph of the code, its history, the people who wrote it, and the decisions behind it.

## The graph

For the demo I indexed the full history of Flask. Four sources go into one graph:

- **Code**, parsed with Python's `ast`: modules, classes, functions, imports, inheritance and calls. Call resolution is type-aware. It follows re-exports (`flask.Flask` → `flask.app.Flask`), `self`, `super()`, annotated parameters and constructor assignments.
- **Git history:** 5,562 commits and 858 people. Merge commits give 1,521 PRs, and `fixes #N` gives issues. `git blame` at HEAD tells us who wrote the lines that still exist, per function.
- **The changelog:** 63 releases and 501 notes. A note like *"Support nesting blueprints. :issue:`593` :pr:`3923`"* links a release to the PR that implemented it and the issue it closed.
- **The test suite, actually run.** Static analysis can't see that `client.get("/")` ends up in `Flask.full_dispatch_request`, because the call goes through Werkzeug. So Tracewise runs the tests under coverage.py with per-test contexts and writes 16,613 `(test)-[:COVERS]->(function)` edges. It turns out 198 tests exercise `full_dispatch_request`; static analysis found zero.

Indexing all of it takes about 12 seconds.

## Questions become traversals

*Why does `Blueprint.register` look the way it does?* is one query:

```cypher
MATCH (f:Function)<-[:CHANGED]-(c:Commit)-[:PART_OF]->(pr:PR)
OPTIONAL MATCH (pr)-[:FIXES]->(i:Issue)
OPTIONAL MATCH (n:ChangeNote)-[:REFERENCES]->(pr)
RETURN c, pr, collect(i), collect(n)
```

The answer comes back as: 28 surviving lines from pgjones' *Nested blueprints* PR #3923, which fixed #593 and #1548 and shipped in 2.0.0. Every step is an edge you can click in the UI.

*What's risky?* uses FalkorDB's built-in PageRank over the call graph, multiplied by churn and author count. *Where are the chokepoints?* is betweenness centrality. *What are the subsystems?* is label propagation over imports. The agent calls these algorithms as tools; it doesn't have to write them.

## Memory is a graph too

Each user gets their own FalkorDB graph for memory, which uses FalkorDB's multigraph support, so isolation is free. Turns are linked into episodes. Facts hang off `Entity` nodes whose keys are the same qualified names used in the code graph, so memory and code join naturally. When a fact changes, say `url_for` used to have 5 callers and now has 7, the new fact `SUPERSEDES` the old one, and the agent tells you what changed since you last asked.

## Agents that coordinate through the graph

The review crew has five roles. A coordinator turns a diff into `Task` nodes. An impact analyst, a test analyst and a history analyst claim tasks concurrently, write `Fact`s back, and leave `HANDOFF` edges for a reviewer, which reads its inputs only from the graph. Claiming is a single Cypher query, and FalkorDB serializes writes per graph, so no two workers ever take the same task. Reviewing three functions takes about 270 ms.

## What I learned

- **Graphs make provenance cheap.** Since every answer is a path, "show your work" is just returning the path, and the UI draws it.
- **Static plus dynamic beats either alone.** The coverage layer turned "untested" into "198 tests" for the most important function in Flask.
- **Put the boring joins in the database.** The scariest code is where PageRank and churn overlap. That's one `CALL algo.pageRank` and one `MATCH`, not a data pipeline.

Code, data model and every query: <repo link>. Demo: <video link>.
