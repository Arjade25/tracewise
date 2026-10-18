"""HTTP API + web UI.   uvicorn tracewise.api:app --port 8000"""
from __future__ import annotations

import re
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import tools as T
from . import jobs
from .agent import PROVIDERS, Agent, llm_config, test_llm
from .crew import review as crew_review
from .db import list_repos, list_users, repo_graph, rows
from .memory import Memory

WEB = Path(__file__).parent / "web"
app = FastAPI(title="Tracewise", version="0.1.0")
_agents: dict[tuple[str, str, str], Agent] = {}
_lock = threading.Lock()


def _clean_user(u: str) -> str:
    u = re.sub(r"[^a-zA-Z0-9_-]", "", u or "")[:40]
    return u or "guest"


class LLM(BaseModel):
    """A user's own LLM settings, sent from their browser with each request and never stored."""
    provider: str = "openrouter"
    api_key: str = Field("", max_length=500)
    base_url: str | None = Field(None, max_length=300)
    model: str | None = Field(None, max_length=200)


class Ask(BaseModel):
    question: str = Field(..., max_length=20000)
    repo: str = "flask"
    user: str = "guest"
    session: str | None = None
    llm: LLM | None = None

    @field_validator("question")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be empty")
        return v.strip()


class Review(BaseModel):
    repo: str = "flask"
    user: str = "guest"
    symbols: list[str] = []
    diff: str | None = None
    crew: bool = True


class Cypher(BaseModel):
    repo: str = "flask"
    query: str


def _repo(name: str) -> str:
    if name not in list_repos():
        raise HTTPException(404, f"repo {name!r} is not indexed")
    return name


@app.get("/api/repos")
def repos():
    out = []
    for r in list_repos():
        g = repo_graph(r)
        info = rows(g.ro_query("MATCH (x:Repo) RETURN x.indexed_at AS indexed_at, x.head_ts AS head_ts"))
        counts = rows(g.ro_query("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n"))
        edges = rows(g.ro_query("MATCH ()-[e]->() RETURN count(e) AS n"))[0]["n"]
        out.append({"name": r, **(info[0] if info else {}), "nodes": {c["label"]: c["n"] for c in counts},
                    "edges": edges})
    return out


@app.post("/api/ask")
def ask(body: Ask):
    repo, user = _repo(body.repo), _clean_user(body.user)
    if body.session and not SID_RE.match(body.session):
        raise HTTPException(400, "bad session id")
    with _lock:
        # No session id: continue this user's latest session instead of starting a new one per request.
        key = (repo, user, body.session or "")
        agent = _agents.get(key)
        if agent is None:
            agent = Agent(repo, user=user, session=body.session)
            _agents[(repo, user, agent.session)] = agent
        _agents[(repo, user, "")] = agent
    return agent.ask(body.question, llm=body.llm.model_dump() if body.llm else None)


@app.post("/api/review")
def review(body: Review):
    repo, user = _repo(body.repo), _clean_user(body.user)
    if body.crew:
        r = crew_review(repo, user, symbols=body.symbols, diff=body.diff)
        if "error" in r:
            raise HTTPException(400, r["error"])
        return r
    return T.review_change(repo, diff=body.diff, symbols=body.symbols)


@app.post("/api/cypher")
def cypher(body: Cypher):
    return T.run_cypher(_repo(body.repo), body.query)


@app.get("/api/tool/{repo}/{name}")
def tool(repo: str, name: str, symbol: str | None = None, a: str | None = None, b: str | None = None):
    repo = _repo(repo)
    fn = T.TOOLS.get(name)
    if not fn or name in ("run_cypher", "review_change"):
        raise HTTPException(404, "unknown tool")
    kwargs = {k: v for k, v in {"symbol": symbol, "a": a, "b": b}.items() if v}
    if name == "person_profile" and symbol:
        kwargs = {"person": symbol}
    if name == "find_symbol" and symbol:
        kwargs = {"query": symbol}
    return fn(repo, **kwargs)


SID_RE = re.compile(r"^[a-f0-9]{8,32}$")


@app.get("/api/sessions/{user}")
def sessions(user: str, repo: str | None = None):
    return Memory(_clean_user(user)).list_sessions(repo=repo or None)


@app.get("/api/sessions/{user}/{sid}")
def session_detail(user: str, sid: str):
    if not SID_RE.match(sid):
        raise HTTPException(400, "bad session id")
    s = Memory(_clean_user(user)).session_turns(sid)
    if not s:
        raise HTTPException(404, "no such conversation")
    return s


@app.delete("/api/sessions/{user}/{sid}")
def session_delete(user: str, sid: str):
    if not SID_RE.match(sid):
        raise HTTPException(400, "bad session id")
    user = _clean_user(user)
    n = Memory(user).delete_session(sid)
    with _lock:
        for k in [k for k in _agents if k[1] == user and (k[2] == sid or _agents[k].session == sid)]:
            _agents.pop(k, None)
    return {"deleted": sid, "turns": n}


@app.get("/api/memory/{user}")
def memory(user: str):
    m = Memory(_clean_user(user))
    sessions = m.q("""MATCH (s:Session) OPTIONAL MATCH (s)-[:HAS_TURN]->(t:Turn)
                      RETURN s.id AS id, s.repo AS repo, s.started AS started, count(t) AS turns
                      ORDER BY started DESC LIMIT 20""")
    facts = m.q("""MATCH (f:Fact)-[:ABOUT]->(e:Entity) OPTIONAL MATCH (f)-[:SUPERSEDES]->(o:Fact)
                   RETURN e.name AS about, e.key AS key, f.kind AS kind, f.text AS text, f.ts AS ts,
                          f.current AS current, f.source AS source, o.text AS replaced
                   ORDER BY ts DESC LIMIT 60""")
    procs = m.q("""MATCH (p:Procedure) RETURN p.intent AS intent, p.uses AS uses, p.sequence AS steps
                   ORDER BY uses DESC""")
    tasks = m.q("""MATCH (t:Task)-[:ASSIGNED_TO]->(:Agent {name: 'coordinator'})
                   RETURN t.id AS id, t.goal AS goal, t.status AS status, t.created AS created
                   ORDER BY created DESC LIMIT 10""")
    return {"stats": m.stats(), "sessions": sessions, "facts": facts, "procedures": procs, "reviews": tasks}


@app.get("/api/memory/{user}/graph")
def memory_graph(user: str, limit: int = 250):
    m = Memory(_clean_user(user))
    res = m.q("""MATCH (a)-[r]->(b)
                 WHERE NOT a:ToolCall AND NOT b:ToolCall
                 RETURN id(a) AS a, labels(a)[0] AS la,
                        coalesce(a.name, a.intent, left(a.question, 40), left(a.text, 40), a.goal, a.id) AS na,
                        id(b) AS b, labels(b)[0] AS lb,
                        coalesce(b.name, b.intent, left(b.question, 40), left(b.text, 40), b.goal, b.id) AS nb,
                        type(r) AS t
                 LIMIT $n""", {"n": limit})
    nodes, edges = {}, []
    for r in res:
        nodes[f"m{r['a']}"] = {"id": f"m{r['a']}", "label": r["la"], "name": str(r["na"])}
        nodes[f"m{r['b']}"] = {"id": f"m{r['b']}", "label": r["lb"], "name": str(r["nb"])}
        edges.append({"source": f"m{r['a']}", "target": f"m{r['b']}", "type": r["t"]})
    return {"nodes": list(nodes.values()), "edges": edges}


@app.get("/api/review/{user}/{task}")
def review_tree(user: str, task: str):
    return Memory(_clean_user(user)).task_tree(task)


class AddRepo(BaseModel):
    url: str = Field(..., max_length=300)
    name: str | None = Field(None, max_length=40)
    rev: str | None = Field(None, max_length=100)


@app.post("/api/repos")
def add_repo(body: AddRepo):
    """Clone a public git repository and index it in the background."""
    name = (body.name or jobs.default_name(body.url)).strip().lower()
    r = jobs.start(body.url, name, rev=(body.rev or None))
    if "error" in r:
        raise HTTPException(400, r["error"])
    return r["job"]


@app.delete("/api/repos/{name}")
def remove_repo(name: str):
    """Delete a repository's graph and its local clone. Users' memory (and History) is kept."""
    name = _repo(name)
    if len(list_repos()) <= 1:
        raise HTTPException(400, "Can't remove the only repository; add another one first")
    if any(j["name"] == name and j["status"] == "running" for j in jobs.JOBS.values()):
        raise HTTPException(409, f"'{name}' is being indexed right now; try again when it finishes")
    repo_graph(name).delete()
    jobs.remove_clone(name)
    with _lock:
        for k in [k for k in _agents if k[0] == name]:
            _agents.pop(k, None)
    return {"ok": True, "repos": list_repos()}


@app.get("/api/users")
def users():
    """People who have used Tracewise: each one has their own memory graph."""
    return list_users()


@app.get("/api/jobs/{jid}")
def job(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "unknown job")
    return j


@app.post("/api/llm/test")
def llm_test(body: LLM):
    return test_llm(body.model_dump())


@app.get("/api/llm/providers")
def llm_providers():
    return PROVIDERS


@app.get("/api/status")
def status():
    """LLM configuration (never the key itself) and the last LLM error, for debugging."""
    return {"repos": list_repos(), "llm": llm_config()}


@app.get("/healthz")
def healthz():
    return {"ok": True, "repos": list_repos()}


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
