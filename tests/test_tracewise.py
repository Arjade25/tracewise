"""End-to-end tests against a real FalkorDB, on a tiny synthetic git repository.

Needs FalkorDB on FALKORDB_HOST:FALKORDB_PORT (default localhost:6379); skipped otherwise.
"""
from __future__ import annotations

import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

from tracewise import db

try:
    db.client().list_graphs()
except Exception:  # pragma: no cover
    pytest.skip("FalkorDB is not reachable", allow_module_level=True)

FILES_V1 = {
    "src/shop/__init__.py": "from .cart import Cart as Cart\n",
    "src/shop/pricing.py": """
        def tax(amount: float) -> float:
            return amount * 0.2

        def total(amount: float) -> float:
            return amount + tax(amount)
    """,
    "src/shop/cart.py": """
        from .pricing import total

        class Base:
            def checkout(self) -> float:
                return 0.0

        class Cart(Base):
            def __init__(self) -> None:
                self.items: list[float] = []

            def add(self, price: float) -> None:
                self.items.append(price)

            def checkout(self) -> float:
                return total(sum(self.items))
    """,
    "tests/test_cart.py": """
        from shop import Cart

        def test_checkout():
            c = Cart()
            c.add(10)
            assert c.checkout() == 12
    """,
    "CHANGES.rst": """
        Version 1.0.0
        -------------

        Released 2026-01-01

        -   Add ``tax`` to totals. :issue:`7` :pr:`8`
    """,
}


def git(repo: Path, *args: str, author: str = "Ada <ada@example.com>") -> None:
    name, email = author[:-1].split(" <")
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
           "GIT_COMMITTER_EMAIL": email, "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(repo)}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("shop")
    git(root, "init", "-q", "-b", "main")
    for path, body in FILES_V1.items():
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial shop")
    # Grace adds tax handling in a PR merged with a "title (#8)" squash commit that fixes #7.
    p = root / "src/shop/pricing.py"
    p.write_text(p.read_text().replace("amount * 0.2", "round(amount * 0.2, 2)"))
    git(root, "commit", "-q", "-am", "Round tax to cents (#8)\n\nfixes #7", author="Grace <grace@example.com>")
    name = "test-" + uuid.uuid4().hex[:8]
    from tracewise.ingest import ingest
    ingest(root, name, log=lambda *a: None)
    yield name
    db.repo_graph(name).delete()


def test_code_layer(repo):
    from tracewise import tools as T
    t = T.Trace(repo)
    calls = {(r["a"], r["b"]) for r in t.q(
        "MATCH (a:Function)-[:CALLS]->(b:Function) RETURN a.qname AS a, b.qname AS b")}
    assert ("shop.pricing.total", "shop.pricing.tax") in calls
    assert ("shop.cart.Cart.checkout", "shop.pricing.total") in calls          # via import
    assert ("tests.test_cart.test_checkout", "shop.cart.Cart.checkout") in calls  # re-export + local type
    inh = t.q("MATCH (:Class {name:'Cart'})-[:INHERITS]->(b:Class) RETURN b.name AS b")
    assert inh == [{"b": "Base"}]


def test_impact_and_tests(repo):
    from tracewise import tools as T
    r = T.impact(repo, "tax")["result"]
    names = {c["qname"] for c in r["callers"]}
    assert {"shop.pricing.total", "shop.cart.Cart.checkout"} <= names
    assert "tests.test_cart.test_checkout" in r["tests_exercising"]
    # An override counts callers of the base method too.
    r = T.impact(repo, "Cart.checkout")["result"]
    assert "shop.cart.Base.checkout" in r["also_dispatches_from"]
    assert T.tests_for(repo, "tax")["result"]["test_count"] == 1


def test_history_and_decisions(repo):
    from tracewise import tools as T
    w = T.why(repo, "tax")["result"]
    top = w["changes"][0]
    assert top["author"] == "Grace" and top["pr"] == 8 and 7 in top["issues"]
    assert any("1.0.0" in n for n in top["notes"])
    assert any(m["version"] == "1.0.0" for m in w["changelog_mentions"])
    ex = T.experts(repo, "tax")["result"]["experts"]
    assert ex[0]["person"] == "Grace"


def test_connection_and_algorithms(repo):
    from tracewise import tools as T
    c = T.connection(repo, "Cart.checkout", "tax")["result"]
    assert c["path"] == ["shop.cart.Cart.checkout", "shop.pricing.total", "shop.pricing.tax"]
    assert T.hotspots(repo)["result"]
    assert "error" in T.run_cypher(repo, "MATCH (n) DETACH DELETE n")["result"]


def test_review_diff(repo):
    from tracewise import tools as T
    diff = "--- a/src/shop/pricing.py\n+++ b/src/shop/pricing.py\n@@ -1,2 +1,2 @@\n def tax(amount):\n-    x\n+    y\n"
    r = T.review_change(repo, diff=diff)["result"]
    assert r["changed_functions"] == ["shop.pricing.tax"]
    assert "Grace" in r["suggested_reviewers"]


def test_memory_supersedes_and_recalls(repo):
    from tracewise.agent import Agent
    from tracewise.memory import Memory
    user = "t" + uuid.uuid4().hex[:8]
    try:
        a = Agent(repo, user=user, mode="offline")
        a.ask("What breaks if I change tax?")
        m = Memory(user)
        first = m.facts_about(["shop.pricing.tax"])
        assert any(f["kind"] == "impact" for f in first)
        r = m.remember(repo, "shop.pricing.tax", "impact", "changed text", {"callers": 99})
        assert r["changed"] and r["previous"]
        timeline = m.fact_timeline("shop.pricing.tax")
        assert sum(1 for f in timeline if f["kind"] == "impact" and f["current"]) == 1
        b = Agent(repo, user=user, mode="offline")  # a new session remembers the old one
        out = b.ask("What breaks if I change tax?")
        assert out["memory"]["earlier"]
        assert m.procedure_for("impact") == ["impact", "experts"]
    finally:
        db.memory_graph(user).delete()


def test_crew_coordinates_through_graph(repo):
    from tracewise.crew import review
    user = "c" + uuid.uuid4().hex[:8]
    try:
        r = review(repo, user, symbols=["tax", "total"])
        assert {s["status"] for s in r["tree"]["subtasks"]} == {"done"}
        assert len(r["tree"]["subtasks"]) == 6
        agents = {h["source"] for h in r["tree"]["handoffs"]}
        assert {"coordinator", "impact-analyst", "test-analyst", "history-analyst"} <= agents
        assert "Grace" in r["reviewers"]
    finally:
        db.memory_graph(user).delete()


@pytest.mark.parametrize("q", [
    "MATCH (n) RETURN n LIMIT 1; MATCH (n) DETACH DELETE n",
    "MATCH (n:Function) WITH n LIMIT 1 CALL { WITH n DETACH DELETE n } RETURN 1",
    "CALL db.idx.fulltext.drop('Function')",
    "MATCH (n:Function) FOREACH (x IN [1] | dElEtE n) RETURN 1",
    "UNWIND [1] AS x CREATE (:Hack)",
    "MATCH (n) SET n.x = 1 RETURN n",
    "CALL db.idx.fulltext.createNodeIndex('Person', 'name')",
])
def test_cypher_guard_blocks_writes(repo, q):
    from tracewise import tools as T
    before = T.Trace(repo).q("MATCH (n) RETURN count(n) AS n")[0]["n"]
    r = T.run_cypher(repo, q)["result"]
    assert isinstance(r, dict) and "error" in r
    assert T.Trace(repo).q("MATCH (n) RETURN count(n) AS n")[0]["n"] == before


def test_cypher_guard_allows_keywords_in_strings(repo):
    from tracewise import tools as T
    r = T.run_cypher(repo, "MATCH (p:Person) WHERE p.name <> 'DELETE me; SET x' RETURN p.name AS n /* CREATE */")["result"]
    assert isinstance(r, list) and {x["n"] for x in r} >= {"Ada", "Grace"}


def test_concurrent_task_claims_are_exclusive(repo):
    """Many workers racing for the same queue: every task is claimed exactly once."""
    import threading
    from tracewise.memory import Memory
    user = "q" + uuid.uuid4().hex[:8]
    try:
        m = Memory(user)
        ids = {m.create_task(f"t{i}", "worker") for i in range(40)}
        claimed, lock = [], threading.Lock()

        def run():
            mine = Memory(user)
            while (t := mine.claim("worker")) is not None:
                with lock:
                    claimed.append(t["id"])

        threads = [threading.Thread(target=run) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert len(claimed) == len(set(claimed)) == len(ids)
        assert set(claimed) == ids
    finally:
        db.memory_graph(user).delete()


def test_reindex_removes_stale_nodes(repo, tmp_path):
    """Rebuilding the graph after a function is deleted leaves no trace of it."""
    import shutil
    from tracewise.ingest import ingest
    from tracewise import tools as T
    src = Path(T.Trace(repo).q("MATCH (r:Repo) RETURN r.path AS p")[0]["p"])
    copy = tmp_path / "shop2"
    shutil.copytree(src, copy)
    p = copy / "src/shop/pricing.py"
    p.write_text(p.read_text().replace("def tax(", "def levy(").replace("tax(amount)", "levy(amount)"))
    git(copy, "commit", "-q", "-am", "rename tax to levy")
    name = repo + "-re"
    try:
        ingest(copy, name, log=lambda *a: None)
        names = {r["n"] for r in T.Trace(name).q("MATCH (f:Function) RETURN f.name AS n")}
        assert "levy" in names and "tax" not in names
        assert T.impact(name, "levy")["result"]["affected_functions"] >= 2
    finally:
        db.repo_graph(name).delete()


def test_procedural_memory_changes_next_answer(repo):
    """A tool sequence learned in one session is replayed for the same kind of question later."""
    from tracewise.agent import Agent
    from tracewise.memory import Memory
    from tracewise import tools as T
    user = "p" + uuid.uuid4().hex[:8]
    try:
        m = Memory(user)
        s = m.start_session(repo)
        # An earlier (e.g. LLM-driven) session answered a 'why' question with why + experts.
        m.record_turn(s, "why is tax like this?", "…", [T.why(repo, "tax"), T.experts(repo, "tax")], intent="why")
        assert m.procedure_for("why") == ["why", "experts"]
        r = Agent(repo, user=user, mode="offline").ask("Why does total look the way it does?")
        tools = [c["tool"] for c in r["tool_calls"]]
        assert tools[0] == "why" and "experts" in tools
        assert any(c["learned"] for c in r["tool_calls"])
        assert "learned from" in r["answer"]
        # A failed turn must not overwrite the learned procedure.
        m.record_turn(s, "why nope?", "…", [T.why(repo, "does_not_exist")], intent="why")
        assert m.procedure_for("why") == ["why", "experts"]
    finally:
        db.memory_graph(user).delete()


def test_conversation_history_list_reopen_delete(repo):
    """Conversations are listed per repo, reopen with their turns, and delete without losing facts."""
    from tracewise.memory import Memory
    from tracewise import tools as T
    user = "h" + uuid.uuid4().hex[:8]
    try:
        m = Memory(user)
        a = m.start_session(repo, "a1b2c3d4e5f60001")
        m.record_turn(a, "What breaks if I change tax?", "tax is used by total", [T.impact(repo, "tax")], intent="impact")
        b = m.start_session("other-repo", "a1b2c3d4e5f60002")
        m.record_turn(b, "hello there", "hi", [], intent="help")
        m.start_session(repo, "a1b2c3d4e5f60003")  # empty: never listed
        m.remember(repo, "fn:tax", "owner", "Ada owns tax")
        assert [s["id"] for s in m.list_sessions(repo)] == [a]
        assert {s["id"] for s in m.list_sessions()} == {a, b}
        assert m.list_sessions(repo)[0]["title"].startswith("What breaks")
        conv = m.session_turns(a)
        assert conv["repo"] == repo and len(conv["turns"]) == 1
        assert conv["turns"][0]["answer"] == "tax is used by total"
        assert conv["turns"][0]["evidence"]["nodes"]
        m.delete_session(a)
        assert m.session_turns(a) is None and m.list_sessions(repo) == []
        assert m.facts_about(["fn:tax"])
    finally:
        db.memory_graph(user).delete()


def test_unknown_names_get_suggestions_not_a_random_answer(repo):
    from tracewise.agent import Agent
    user = "u" + uuid.uuid4().hex[:8]
    try:
        a = Agent(repo, user=user, mode="offline")
        r = a.ask("What breaks if I change taxx_amount?")
        assert "couldn't find" in r["answer"] and r["tool_calls"] == []
        r = a.ask("Why does totl look like this?")  # plain word that resolves to nothing
        assert r["intent"] == "why"
        r = a.ask("hello")
        assert r["intent"] == "help" and "Try:" in r["answer"]
        r = a.ask("What does Nobody McNobodyface own?")
        assert "couldn't find a contributor" in r["answer"]
    finally:
        db.memory_graph(user).delete()


def test_close_name_suggestions(repo):
    from tracewise import tools as T
    assert "shop.pricing.tax" in T.suggest(repo, "taxx")
    assert "shop.pricing.total" in T.suggest(repo, "totl")


def test_api_rejects_empty_questions_and_reuses_sessions(repo):
    from fastapi.testclient import TestClient
    from tracewise.api import app
    c = TestClient(app)
    assert c.post("/api/ask", json={"question": "   ", "repo": repo}).status_code == 422
    user = "s" + uuid.uuid4().hex[:8]
    try:
        s1 = c.post("/api/ask", json={"question": "who knows tax", "repo": repo, "user": user}).json()["session"]
        s2 = c.post("/api/ask", json={"question": "who knows total", "repo": repo, "user": user}).json()["session"]
        assert s1 == s2
    finally:
        db.memory_graph(user).delete()


def test_remove_repo_and_list_users(repo):
    from fastapi.testclient import TestClient
    from tracewise.api import app
    from tracewise.memory import Memory
    c = TestClient(app)
    tmp, user = "rm" + uuid.uuid4().hex[:6], "u" + uuid.uuid4().hex[:8]
    try:
        db.repo_graph(tmp).query("CREATE (:Repo {name: $n})", {"n": tmp})
        Memory(user).start_session(tmp)
        assert user in c.get("/api/users").json()
        assert c.delete(f"/api/repos/{tmp}").status_code == 200
        assert tmp not in db.list_repos() and repo in db.list_repos()
        assert c.delete(f"/api/repos/{tmp}").status_code == 404
        assert user in c.get("/api/users").json()  # memory survives the repo
    finally:
        if tmp in db.list_repos():
            db.repo_graph(tmp).delete()
        db.memory_graph(user).delete()


def test_user_key_overrides_server_and_is_never_echoed(monkeypatch):
    from tracewise.agent import resolve_llm
    monkeypatch.setenv("TRACEWISE_API_KEY", "sk-proj-server")
    monkeypatch.setenv("TRACEWISE_BASE_URL", "https://example.invalid/v1")
    cfg = resolve_llm({"provider": "openrouter", "api_key": ' "sk-or-v1-mine"\r\n'})
    assert cfg["api_key"] == "sk-or-v1-mine" and cfg["source"] == "browser"
    assert cfg["base_url"] == "https://openrouter.ai/api/v1" and cfg["model"] == "openai/gpt-4.1-mini"
    assert resolve_llm({"provider": "openrouter", "api_key": ""})["source"] == "server"   # empty key -> server's
    monkeypatch.delenv("TRACEWISE_API_KEY")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert resolve_llm(None) is None


def test_add_repo_validation():
    from tracewise import jobs
    assert jobs.validate("https://github.com/pallets/click", "click") is None
    assert jobs.validate("file:///etc/passwd", "x")
    assert jobs.validate("git@github.com:pallets/click.git", "x")
    assert jobs.validate("https://github.com/pallets/click; rm -rf /", "x")
    assert jobs.validate("https://github.com/pallets/click", "../../etc")
    assert jobs.default_name("https://github.com/psf/Requests.git") == "requests"
    # versions: tags, branches and shas pass; git options and path tricks don't
    for ok in ["8.1.3", "main", "release/2.0", "a1b2c3d"]:
        assert jobs.validate("https://github.com/pallets/click", "click", ok) is None
    for bad in ["--upload-pack=touch /tmp/x", "-x", "v1..v2", "a b", "8.1.3;ls"]:
        assert jobs.validate("https://github.com/pallets/click", "click", bad)


def test_changelog_formats(tmp_path):
    from tracewise.ingest.history import parse_changelog
    md = tmp_path / "CHANGES.md"
    md.write_text("## Version 2.0.0\n\nReleased 2024-01-02\n\n- Add `do_thing`. {pr}`12` {issue}`7`\n"
                  "  continued line\n\n## [1.0.0] - 2023-05-01\n\n* First release (#3)\n")
    r = parse_changelog(md)
    assert [x["version"] for x in r] == ["2.0.0", "1.0.0"]
    assert r[0]["date"] == "2024-01-02" and r[1]["date"] == "2023-05-01"
    assert r[0]["notes"][0].endswith("continued line")
