"""The Tracewise agent.

Two modes share the same graph tools and the same memory graph:

* ``llm``     – a tool-use loop with Claude (Anthropic API) or any OpenAI-compatible
                model (OpenAI, Groq, OpenRouter, Ollama...). The model picks tools.
* ``offline`` – a deterministic router: intent + symbol extraction picks the tools,
                answers are rendered from templates. No API key needed.

Each turn: recall memory -> run tools -> answer -> write episodic + semantic memory.
Facts carry values, so when the graph changes between sessions the agent says so.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

from . import tools as T
from .memory import Memory

# Last LLM failure, surfaced by /api/status and the UI badge so misconfiguration is visible.
LLM_STATUS: dict = {"last_error": None, "last_error_at": None, "last_ok_at": None}


def _env(name: str) -> str | None:
    """TRACEWISE_<NAME> wins over the generic variable, so machine-wide OpenAI settings don't leak in."""
    return os.environ.get("TRACEWISE_" + name) or os.environ.get("OPENAI_" + name)


def _clean(v: str | None) -> str:
    """Strip whitespace, CR/LF and wrapping quotes that Windows editors and .env files leave behind."""
    return (v or "").strip().strip('"').strip("'").strip()


def _key_check(raw: str | None) -> dict | None:
    if not raw:
        return None
    k = _clean(raw)
    return {"length": len(k), "prefix": k[:8], "had_junk": k != raw,
            "looks_like_openrouter": k.startswith("sk-or-"), "placeholder": "..." in k or "your" in k.lower()}


PROVIDERS = {
    "openrouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4.1-mini"},
    "openai": {"kind": "openai", "base_url": None, "model": "gpt-4.1-mini"},
    "anthropic": {"kind": "anthropic", "base_url": None, "model": "claude-sonnet-5-5"},
    "groq": {"kind": "openai", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
    "custom": {"kind": "openai", "base_url": None, "model": None},
}


def resolve_llm(override: dict | None = None) -> dict | None:
    """The LLM to use for one request: the user's own key (sent from their browser) if given,
    otherwise the server's environment. Returns None when no key is available."""
    if override and _clean(override.get("api_key")):
        preset = PROVIDERS.get((override.get("provider") or "custom").lower(), PROVIDERS["custom"])
        return {"kind": preset["kind"], "provider": (override.get("provider") or "custom").lower(),
                "api_key": _clean(override["api_key"]),
                "base_url": _clean(override.get("base_url")) or preset["base_url"],
                "model": _clean(override.get("model")) or preset["model"] or "gpt-4.1-mini",
                "source": "browser"}
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"kind": "anthropic", "provider": "anthropic", "api_key": _clean(os.environ["ANTHROPIC_API_KEY"]),
                "base_url": None, "model": os.environ.get("TRACEWISE_MODEL") or "claude-sonnet-5-5", "source": "server"}
    if _env("API_KEY"):
        return {"kind": "openai", "provider": "openai-compatible", "api_key": _clean(_env("API_KEY")),
                "base_url": _clean(_env("BASE_URL")) or None,
                "model": os.environ.get("TRACEWISE_MODEL") or "gpt-4.1-mini", "source": "server"}
    return None


def test_llm(override: dict | None) -> dict:
    """Make one tiny request to check a key/model works. Never returns the key."""
    cfg = resolve_llm(override)
    if not cfg:
        return {"ok": False, "error": "no API key provided"}
    t0 = time.time()
    try:
        if cfg["kind"] == "anthropic":
            import anthropic
            r = anthropic.Anthropic(api_key=cfg["api_key"]).messages.create(
                model=cfg["model"], max_tokens=5, messages=[{"role": "user", "content": "Reply with OK."}])
            text = "".join(b.text for b in r.content if b.type == "text")
        else:
            from openai import OpenAI
            r = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"]).chat.completions.create(
                model=cfg["model"], max_tokens=5, messages=[{"role": "user", "content": "Reply with OK."}])
            text = r.choices[0].message.content or ""
        return {"ok": True, "model": cfg["model"], "provider": cfg["provider"], "reply": text[:40],
                "ms": round((time.time() - t0) * 1000)}
    except Exception as exc:
        return {"ok": False, "model": cfg["model"], "provider": cfg["provider"],
                "error": f"{type(exc).__name__}: {str(exc)[:400]}"}


def llm_config() -> dict:
    base = _env("BASE_URL") or ""
    provider = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else ("openai-compatible" if _env("API_KEY") else None)
    return {
        "provider": provider,
        "forced_mode": os.environ.get("TRACEWISE_MODE") or None,
        "model": os.environ.get("TRACEWISE_MODEL") or None,
        "base_url": base.split("?")[0] or None,
        "key_present": bool(os.environ.get("ANTHROPIC_API_KEY") or _env("API_KEY")),
        "key_source": "TRACEWISE_API_KEY" if os.environ.get("TRACEWISE_API_KEY") else (
            "OPENAI_API_KEY" if os.environ.get("OPENAI_API_KEY") else None),
        "key_check": _key_check(_env("API_KEY") or os.environ.get("ANTHROPIC_API_KEY")),
        **LLM_STATUS,
    }


# ----------------------------------------------------------- tool schemas
SCHEMAS = [
    ("find_symbol", "Search functions/classes by (partial) name. Use first when unsure of a name.",
     {"query": ("string", "name or fragment, e.g. 'dispatch'")}),
    ("impact", "Blast radius of changing a function, class or module: transitive callers, affected modules, "
               "risk level, and tests that exercise it.",
     {"symbol": ("string", "function/class/module name"), "depth": ("integer", "call depth 1-5, default 3")}),
    ("tests_for", "Tests that execute a symbol (measured coverage + static call paths) and a pytest command.",
     {"symbol": ("string", "function/class/module name")}),
    ("experts", "Who knows this code best: surviving-line authorship, nearby code, recent commits, merged PRs.",
     {"symbol": ("string", "function/class/module name")}),
    ("why", "Decision trace: commits that shaped this code, their PRs, fixed issues and release notes.",
     {"symbol": ("string", "function/class/module name")}),
    ("connection", "Shortest structural path between two pieces of code.",
     {"a": ("string", "first symbol"), "b": ("string", "second symbol")}),
    ("hotspots", "Riskiest code: PageRank over the call graph x git churn x author count.", {}),
    ("bridges", "Chokepoint functions with the highest betweenness centrality in the call graph.", {}),
    ("components", "Subsystems found by community detection over the module import graph.", {}),
    ("recent_activity", "What changed recently, where, by whom, in which PRs.",
     {"days": ("integer", "lookback window in days, default 120")}),
    ("person_profile", "What a contributor owns and works on.", {"person": ("string", "name or fragment")}),
    ("review_change", "Review a proposed change (unified diff or list of functions): risk, reviewers, tests to "
                      "run, prior decisions. Returns a markdown review plan.",
     {"diff": ("string", "unified diff text"), "symbols": ("array", "changed function names")}),
    ("graph_schema", "Node labels and relationship types with counts.", {}),
    ("run_cypher", "Read-only Cypher against the repo graph for anything the other tools miss. Labels: Module, "
                   "Class, Function, Person, Commit, PR, Issue, Release, ChangeNote. Rels: DEFINES, HAS_METHOD, "
                   "INHERITS, IMPORTS, CALLS, INSTANTIATES, COVERS, AUTHORED, MODIFIED, CHANGED, WROTE, PART_OF, "
                   "FIXES, HAS_NOTE, REFERENCES, MENTIONS.",
     {"query": ("string", "a read-only Cypher query")}),
    ("recall", "Search this user's memory: earlier questions and stored facts about a symbol or person.",
     {"symbol": ("string", "symbol or person to recall")}),
]


def _schema(name, desc, props, flavor):
    required = [k for k in props if k not in ("depth", "days", "diff", "symbols")]
    p = {}
    for k, (typ, d) in props.items():
        p[k] = {"type": typ, "description": d}
        if typ == "array":
            p[k]["items"] = {"type": "string"}
    params = {"type": "object", "properties": p, "required": required}
    if flavor == "anthropic":
        return {"name": name, "description": desc, "input_schema": params}
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}


SYSTEM = """You are Tracewise, an engineering agent that answers questions about a codebase by \
querying a FalkorDB knowledge graph of its code, history, people and decisions.

Rules:
- Act, don't ask: call the tools you need right away. Never ask the user for permission to search or look something up; if a name is ambiguous, use find_symbol and pick the best match.
- Ground every claim in tool results. Never guess names, owners, PR numbers or counts.
- Questions about what you remember, earlier sessions or "last time": call recall for the symbol. Never claim to have no memory without calling recall first.
- Follow-ups ("yes", "go ahead", "explain how it's used", "and who wrote it?") refer to the previous turns of this conversation; resolve them from that context.
- Show how you got the answer: name the graph path (e.g. function -> commit -> PR #123 -> release 3.0.0).
- Use memory: if earlier facts are provided and the graph now says something different, point out what changed.
- Be concise. Use short markdown; include a pytest command or reviewer names when relevant.

Repository: {repo}
{memory}"""


# --------------------------------------------------------------- intents
INTENTS = [
    ("review", r"\breview\b|\bdiff\b|\bpr\b.*(change|risk)|\bpatch\b"),
    ("connection", r"\b(connect(ed|ion)?|path|relate[sd]?|link(ed)?)\b.*\b(and|to|between)\b"),
    ("experts", r"\bwho\b.*\b(know|own|wrote|expert|ask|review|maintain)|\bexpert|\bowner"),
    ("why", r"\bwhy\b|\bhistory\b|\bdecision|\bintroduc|\boriginally|\bhow did\b|\bwhen was\b"),
    ("tests", r"\btests?\b|\bcoverage\b|\bcovered\b"),
    ("impact", r"\bbreak|\bimpact|\bblast|\baffect|\bdepend|\bcall(s|ers|ed)?\b|\bchange\b|\brefactor"),
    ("hotspots", r"\bhot ?spot|\brisk(y|iest)?\b|\bcritical\b|\bfragile\b|\bimportant\b"),
    ("bridges", r"\bbottleneck|\bchoke|\bbetweenness|\bcentral"),
    ("components", r"\bsubsystem|\bcomponent|\bcommunit|\barchitecture|\bmodules? group"),
    ("recent", r"\brecent|\blately\b|\bthis (month|week)|\bactivity"),
    ("person", r"\bwhat (does|has) .* (work|own)|\bprofile\b|\bcontribut"),
    ("recall", r"\b(last time|before|earlier|remember|previously|we (looked|talked|discussed))\b"),
]
IDENT = re.compile(r"`([^`]+)`|\b([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)+|[a-z]+_[a-z_]+\w*|[A-Z][a-z]+[A-Z]\w*|"
                   r"[\w/]+\.py)\b")
STOP = set("""a an and are about any ask be before break breaks by can change changed changes code connected
connection cover covered covers did do does earlier for from get has have how i if in into is it its last like
look looks me my of on or remember review risk riskiest should test tests that the their them they this to
under was way we what when where which who why will with would you your parts part codebase function class
module file files work owns own does do done""".split())


def classify(question: str) -> str:
    q = question.lower()
    for name, pat in INTENTS:
        if re.search(pat, q):
            return name
    return "impact" if IDENT.search(question) else "help"


def symbols_in(question: str, repo: str) -> list[dict]:
    found, seen = [], set()
    cands = [a or b for a, b in IDENT.findall(question)]
    # Capitalised words ("Blueprint", "Flask") are only candidates if they resolve.
    cands += re.findall(r"\b([A-Z][a-zA-Z]{3,})\b", question)
    cands += [w for w in re.findall(r"\b([a-z][a-z0-9_]{2,})\b", question) if w not in STOP]
    t = T.Trace(repo)
    explicit = {a or b for a, b in IDENT.findall(question)}
    for c in cands:
        if c.lower() in STOP or c in seen:
            continue
        seen.add(c)
        r = T.resolve(t, c)
        # A plain English word ("hello", "Session") only counts as code if it names library code,
        # not something in the examples or the test suite.
        if r and c not in explicit and (r.get("path") or "").startswith(("examples/", "tests/", "docs/")):
            continue
        if r and r["key"] not in {f["key"] for f in found}:
            found.append({**r, "mention": c})
        if len(found) >= 3:
            break
    return found


CODEISH = re.compile(r"`([^`]+)`|\b([A-Za-z_]\w*\.\w[\w.]*|[a-z0-9]+_[a-z0-9_]+|[a-z]+[A-Z]\w*|[A-Z][a-z]+[A-Z]\w*)(?:\(\))?")


def unresolved_names(question: str, found: list[dict]) -> list[str]:
    """Code-looking words in the question (snake_case, dotted, CamelCase, `backticked`) that matched nothing."""
    seen = {f["mention"] for f in found} | {f["key"].rsplit(".", 1)[-1] for f in found}
    out = []
    for a, b in CODEISH.findall(question):
        w = (a or b).strip()
        if w and w not in seen and w.lower() not in STOP and not w.endswith(".py") and w not in out:
            out.append(w)
    return out


def person_in(question: str, repo: str) -> str | None:
    t = T.Trace(repo)
    for w in re.findall(r"\b([A-Z][\w\-]+(?: [A-Z][\w\-]+)?)\b", question):
        r = t.q("MATCH (p:Person) WHERE p.name = $n OR toLower(p.name) STARTS WITH toLower($n) "
                "OPTIONAL MATCH (p)-[:AUTHORED]->(c) RETURN p.name AS name, count(c) AS n ORDER BY n DESC LIMIT 1",
                {"n": w})
        if r and r[0]["n"] > 0:
            return r[0]["name"]
    return None


# ------------------------------------------------------------ fact store
def facts_from(call: dict) -> list[dict]:
    """Turn a tool result into durable facts (with values, so drift can be detected)."""
    r, tool, out = call["result"], call["tool"], []
    if not isinstance(r, dict) or "error" in r:
        return out
    tgt = r.get("target") or {}
    key, label = tgt.get("key"), tgt.get("label")
    if tool == "impact" and key:
        out.append({"key": key, "label": label, "kind": "impact",
                    "text": f"{r['affected_functions']} production callers across {len(r['affected_modules'])} "
                            f"modules, {r['test_count']} tests, risk {r['risk']}",
                    "value": {"callers": r["affected_functions"], "tests": r["test_count"], "risk": r["risk"]}})
    elif tool == "experts" and key and r.get("experts"):
        names = [e["person"] for e in r["experts"][:3] if e.get("active", True)] or [e["person"] for e in r["experts"][:3]]
        out.append({"key": key, "label": label, "kind": "experts", "text": "Best people to ask: " + ", ".join(names),
                    "value": names})
    elif tool == "why" and key and r.get("changes"):
        c = r["changes"][0]
        pr = f"PR #{c['pr']} ({c['pr_title']})" if c.get("pr") else f"commit {c['commit']}"
        out.append({"key": key, "label": label, "kind": "origin",
                    "text": f"Mostly shaped by {pr} by {c['author']} on {c['ts']}",
                    "value": [x.get("pr") or x["commit"] for x in r["changes"][:3]]})
    elif tool == "tests_for" and key:
        out.append({"key": key, "label": label, "kind": "tests",
                    "text": f"{r['test_count']} tests execute it" if r["test_count"] else "No test reaches it",
                    "value": r["test_count"]})
    elif tool == "review_change":
        for f in r.get("per_function", []):
            out.append({"key": f["function"], "label": "Function", "kind": "review",
                        "text": f"Reviewed: risk {f['risk']}, {f['affected_functions']} callers, {f['tests']} tests",
                        "value": {"risk": f["risk"], "callers": f["affected_functions"]}})
    return out


# -------------------------------------------------------------- the agent
class Agent:
    def __init__(self, repo: str, user: str = "default", session: str | None = None,
                 mode: str | None = None, model: str | None = None):
        self.repo, self.user = repo, user
        self.mem = Memory(user)
        self.session = self.mem.start_session(repo, session)
        self._mode_arg, self._model_arg = mode, model
        self.configure(None)

    def configure(self, llm: dict | None) -> None:
        """Pick the LLM for the next request: the user's key if they sent one, else the server's."""
        self.llm = resolve_llm(llm)
        if self.llm and self._model_arg:
            self.llm["model"] = self._model_arg
        self.provider = self.llm["kind"] if self.llm else None
        self.model = self.llm["model"] if self.llm else None
        forced = (os.environ.get("TRACEWISE_MODE") or "").strip().lower()
        if self.llm and self.llm["source"] == "browser":
            forced = ""  # a user who adds their own key wants LLM mode
        self.mode = self._mode_arg or (forced if forced in ("llm", "offline") else None) or (
            "llm" if self.llm else "offline")
        if self.mode == "llm" and not self.llm:
            self.mode = "offline"

    # -- tools
    def call_tool(self, name: str, args: dict) -> dict:
        t0 = time.time()
        if name == "recall":
            sym = T.resolve(T.Trace(self.repo), args.get("symbol", "")) or {}
            key = sym.get("key") or args.get("symbol", "")
            res = {"tool": "recall", "args": args, "result": {
                "facts": self.mem.fact_timeline(key) if key else [],
                "earlier_questions": self.mem.history_about([key], exclude_session=None),
            }, "evidence": {"nodes": [], "edges": []}, "cypher": []}
        else:
            fn = T.TOOLS.get(name)
            if not fn:
                return {"tool": name, "args": args, "result": {"error": f"unknown tool {name}"},
                        "evidence": {"nodes": [], "edges": []}, "cypher": []}
            try:
                res = fn(self.repo, **args)
            except TypeError as exc:
                res = {"tool": name, "args": args, "result": {"error": str(exc)},
                       "evidence": {"nodes": [], "edges": []}, "cypher": []}
        res["ms"] = round((time.time() - t0) * 1000, 1)
        return res

    # -- memory
    def memory_context(self, keys: list[str]) -> tuple[str, dict]:
        recent = self.mem.recent_turns(self.session, 4)
        facts = self.mem.facts_about(keys) if keys else []
        earlier = self.mem.history_about(keys, exclude_session=self.session, limit=3) if keys else []
        parts = []
        if recent:
            parts.append("This session so far:\n" + "\n".join(
                f"- Q: {r['question'][:160]}\n  A: {r['answer'][:240]}" for r in recent))
        if facts:
            parts.append("Known facts (from memory):\n" + "\n".join(f"- {f['key']}: [{f['kind']}] {f['text']}"
                                                                     for f in facts[:12]))
        if earlier:
            parts.append("Earlier sessions touched this:\n" + "\n".join(
                f"- \"{e['question'][:140]}\"" for e in earlier))
        return ("\n\n".join(parts) if parts else "(no memory yet)"), {
            "recent": recent, "facts": facts, "earlier": earlier}

    def _store(self, question: str, answer: str, calls: list[dict], intent: str | None,
               mode: str | None = None) -> list[dict]:
        drift = []
        for c in calls:
            for f in facts_from(c):
                r = self.mem.remember(self.repo, f["key"], f["kind"], f["text"], f["value"], source="tool:" + c["tool"],
                                      label=f.get("label"), name=T.short(f["key"]))
                if r["changed"]:
                    drift.append({"key": f["key"], "kind": f["kind"], "before": r["previous"], "now": f["text"]})
        self.mem.record_turn(self.session, question, answer, calls, intent, mode=mode)
        return drift

    # -- entry point
    def ask(self, question: str, on_event: Callable[[dict], None] | None = None,
            llm: dict | None = None) -> dict:
        self.configure(llm)
        emit = on_event or (lambda e: None)
        llm_error = None
        t0 = time.time()
        intent = classify(question)
        syms = symbols_in(question, self.repo)
        keys = [s["key"] for s in syms]
        mem_text, mem = self.memory_context(keys)
        # Procedural memory: the tool sequence that answered this kind of question before.
        proc = self.mem.procedure_info(intent)
        mem["procedure"] = proc
        if proc:
            mem_text += (f"\n\nLearned procedure for '{intent}' questions (succeeded {proc['uses']}x before): "
                         f"{' -> '.join(proc['steps'])}. Start there unless the question needs something else.")
        emit({"type": "memory", "data": mem})
        if self.mode == "llm":
            try:
                answer, calls = self._ask_llm(question, mem_text, emit)
                mode_used = "llm"
                if self.llm["source"] == "server":
                    LLM_STATUS["last_ok_at"] = int(time.time())
            except Exception as exc:  # LLM unreachable or misconfigured: answer from the graph anyway
                llm_error = f"{type(exc).__name__}: {str(exc)[:600]}"
                print(f"[tracewise] LLM call failed ({type(exc).__name__}); using offline mode", flush=True)
                if self.llm["source"] == "server":
                    LLM_STATUS["last_error"] = llm_error
                    LLM_STATUS["last_error_at"] = int(time.time())
                answer, calls = self._ask_offline(question, intent, syms, mem, emit)
                mode_used = "offline (LLM unavailable)"
        else:
            answer, calls = self._ask_offline(question, intent, syms, mem, emit)
            mode_used = "offline"
        if mode_used != "llm" and proc and syms:
            answer += self._replay_procedure(proc, calls, syms[0]["mention"], emit)
        if mode_used != "llm" and syms and syms[0].get("alternatives") and "." not in syms[0]["mention"]:
            alts = ", ".join(f"`{T.short(a)}`" for a in syms[0]["alternatives"][:4])
            answer += (f"\n\n_I picked `{T.short(syms[0]['key'])}`. Other things are also named "
                       f"`{syms[0]['mention']}`: {alts}. Use the fuller name (e.g. `{T.short(syms[0]['alternatives'][0])}`) to choose one._")
        drift = self._store(question, answer, calls, intent, mode=mode_used)
        if drift:
            answer += "\n\n**Changed since you last asked:**\n" + "\n".join(
                f"- `{T.short(d['key'])}` {d['kind']}: was *{d['before']}*, now *{d['now']}*" for d in drift)
        nodes, edges = {}, set()
        for c in calls:
            for n in c["evidence"]["nodes"]:
                nodes.setdefault(n["id"], n)
            for e in c["evidence"]["edges"]:
                edges.add((e["source"], e["target"], e["type"]))
        return {
            "answer": answer, "mode": mode_used,
            "llm_error": llm_error,
            "llm_source": self.llm["source"] if self.llm and mode_used == "llm" else None,
            "model": self.model if mode_used == "llm" else None, "intent": intent, "session": self.session,
            "symbols": syms, "memory": mem, "drift": drift,
            "tool_calls": [{"tool": c["tool"], "args": c["args"], "ms": c.get("ms"), "cypher": c["cypher"],
                            "learned": c.get("learned", False)}
                           for c in calls],
            "evidence": {"nodes": list(nodes.values()),
                         "edges": [{"source": a, "target": b, "type": t} for a, b, t in edges]},
            "ms": round((time.time() - t0) * 1000),
        }

    def _replay_procedure(self, proc: dict, calls: list[dict], symbol: str, emit) -> str:
        """Run the learned steps the router didn't, so earlier sessions shape this answer."""
        done = {c["tool"] for c in calls}
        extra = [t for t in proc["steps"] if t not in done and t in ("impact", "experts", "why", "tests_for")][:2]
        lines = []
        for tool in extra:
            emit({"type": "tool_start", "tool": tool, "args": {"symbol": symbol}})
            c = self.call_tool(tool, {"symbol": symbol})
            c["learned"] = True
            calls.append(c)
            for f in facts_from(c):
                lines.append(f"- {tool}: {f['text']}")
        if not lines:
            return ""
        return (f"\n\n**Also checked** (learned from {proc['uses']} earlier '{proc['intent']}' questions):\n"
                + "\n".join(lines))

    # ------------------------------------------------------------ offline
    def _ask_offline(self, q: str, intent: str, syms: list[dict], mem: dict, emit) -> tuple[str, list[dict]]:
        calls: list[dict] = []

        def run(name, **args):
            emit({"type": "tool_start", "tool": name, "args": args})
            c = self.call_tool(name, args)
            calls.append(c)
            emit({"type": "tool_end", "tool": name, "ms": c["ms"], "cypher": c["cypher"]})
            return c["result"]

        s = syms[0]["mention"] if syms else None
        missing = unresolved_names(q, syms)
        symbol_intents = ("impact", "why", "experts", "tests", "connection", "review")
        if missing and not syms and intent in symbol_intents + ("hotspots",):
            lines = []
            for m in missing[:3]:
                close = T.suggest(self.repo, m)
                lines.append(f"- `{m}`: " + (("closest matches: " + ", ".join(f"`{T.short(c)}`" for c in close))
                                             if close else "no similar names"))
            return (f"I couldn't find {'that name' if len(missing) == 1 else 'those names'} in the `{self.repo}` "
                    f"graph. It may be misspelled, or it may not exist at the indexed revision.\n\n"
                    + "\n".join(lines) + "\n\nAsk again with one of these names."), calls
        if intent == "help" and not syms:
            return ("I answer questions about the **" + self.repo + "** codebase from its graph of code, history, "
                    "people and decisions. Try:\n\n"
                    "- *What breaks if I change `full_dispatch_request`?* (callers, risk, tests)\n"
                    "- *Which tests cover `url_for`?* (measured coverage, with a pytest command)\n"
                    "- *Why does `Blueprint.register` look the way it does?* (commits, PRs, issues, releases)\n"
                    "- *Who should I ask about `save_session`?* (reviewer recommendations)\n"
                    "- *What are the riskiest parts of the codebase?* (PageRank × churn)\n"
                    "- *Review a change to `url_for`*, or paste a `git diff`\n"
                    "- *What do you remember about `url_for`?*"), calls
        if intent == "recall":
            if s:
                r = run("recall", symbol=s)
                facts = r["facts"]
                if not facts and not r["earlier_questions"]:
                    return f"I have nothing stored about `{s}` yet.", calls
                lines = [f"Here is what I remember about `{s}`:"]
                lines += [f"- {'' if f['current'] else '~~'}{f['kind']}: {f['text']}{'' if f['current'] else '~~'}"
                          for f in facts]
                lines += [f"- You asked: \"{e['question']}\"" for e in r["earlier_questions"][:3]]
                return "\n".join(lines), calls
            turns = mem["recent"] or self.mem.q(
                "MATCH (s:Session)-[:HAS_TURN]->(t:Turn) RETURN t.question AS question ORDER BY t.ts DESC LIMIT 5")
            return "Recent questions:\n" + "\n".join(f"- {t['question']}" for t in turns), calls

        if intent == "connection" and len(syms) >= 2:
            r = run("connection", a=syms[0]["mention"], b=syms[1]["mention"])
            if r.get("path"):
                chain = " → ".join(f"`{T.short(p)}`" for p in r["path"])
                return (f"`{T.short(syms[0]['key'])}` and `{T.short(syms[1]['key'])}` are {r['hops']} hops apart "
                        f"({r['direction']}):\n\n{chain}\n\nEdges: {', '.join(r['relationships'])}"), calls
            shared = r.get("shared_dependencies") or []
            return ("No direct path between them." + (" They both depend on: " + ", ".join(
                f"`{T.short(x)}`" for x in shared) if shared else "")), calls

        if intent == "review":
            names = [x["mention"] for x in syms]
            diff = q if "@@" in q else None
            r = run("review_change", diff=diff, symbols=names)
            return r.get("markdown") or r.get("error", "Nothing to review."), calls

        if intent == "person":
            p = person_in(q, self.repo)
            if not p:
                return ("I couldn't find a contributor by that name in the history. Names come from git "
                        "authors, e.g. *David Lord* or *Armin Ronacher*."), calls
            if p:
                r = run("person_profile", person=p)
                owns = ", ".join(f"`{T.short(o['qname'])}` ({o['lines']} lines)" for o in r["owns_most_of"][:6])
                mods = ", ".join(f"{m['module']} ({m['commits']})" for m in r["modules"][:5])
                return (f"**{r['name']}** has {r['commits']} commits.\n\nStill owns most of: {owns}\n\n"
                        f"Most-touched modules: {mods}"), calls

        if intent in ("hotspots", "bridges", "components", "recent") or not s:
            if intent == "bridges":
                r = run("bridges")
                return "Chokepoints (betweenness centrality on the call graph):\n" + "\n".join(
                    f"{i+1}. `{T.short(x['qname'])}` — score {x['score']:.0f}" for i, x in enumerate(r)), calls
            if intent == "components":
                r = run("components")
                return "Subsystems (label propagation over imports):\n" + "\n".join(
                    f"- **{len(c['modules'])} modules**: " + ", ".join(m.split('/')[-1] for m in c["modules"][:8])
                    for c in r), calls
            if intent == "recent":
                r = run("recent_activity")
                return "Recent activity (last 120 days of history):\n" + "\n".join(
                    f"- `{x['module']}`: {x['commits']} commits by {', '.join(x['people'])}"
                    + (f" (PRs {', '.join('#' + str(p) for p in x['prs'])})" if x["prs"] else "") for x in r[:10]), calls
            r = run("hotspots")
            return ("Riskiest code (PageRank × churn × authors):\n" + "\n".join(
                f"{i+1}. `{T.short(x['qname'])}` — {x['commits']} commits, {x['authors']} authors, "
                f"fan-in {x['fan_in']}" for i, x in enumerate(r[:8]))), calls

        if intent == "experts":
            r = run("experts", symbol=s)
            if "error" in r:
                return r["error"], calls
            lines = [f"Suggested people to ask about `{T.short(r['target']['key'])}` (a recommendation, from the graph):"]
            for i, e in enumerate(r["experts"]):
                lines.append(f"{i+1}. **{e['person']}**{'' if e['active'] else ' *(inactive)*'} — " + "; ".join(e["evidence"]))
            lines.append("\n_Authorship isn't the same as current expertise: inactive contributors are down-weighted._")
            return "\n".join(lines), calls

        if intent == "why":
            r = run("why", symbol=s)
            if "error" in r:
                return r["error"], calls
            lines = [f"How `{T.short(r['target']['key'])}` got its current shape:"]
            for c in r["changes"][:5]:
                via = f"PR #{c['pr']} *{c['pr_title']}*" if c["pr"] else f"commit {c['commit']}"
                lines.append(f"- **{c['ts']}** {c['author']}: {c['subject']} ({c['lines']:.0f} lines) — via {via}"
                             + (f", fixes {', '.join('#' + str(i) for i in c['issues'])}" if c["issues"] else ""))
                for n in c["notes"][:1]:
                    v, _, text = n.partition(" | ")
                    lines.append(f"  - Release {v}: {text[:220]}")
            if r["changelog_mentions"]:
                lines.append("\nChangelog mentions:")
                lines += [f"- {m['version']}: {m['note'][:200]}" for m in r["changelog_mentions"][:4]]
            return "\n".join(lines), calls

        if intent == "tests":
            r = run("tests_for", symbol=s)
            if "error" in r:
                return r["error"], calls
            if r["untested"]:
                return f"⚠️ No test executes `{T.short(r['target']['key'])}`.", calls
            measured = sum(1 for x in r["tests"] if x["source"] == "coverage")
            return (f"{r['test_count']} tests reach `{T.short(r['target']['key'])}` ({measured} measured by "
                    f"coverage) across {len(r['test_files'])} files.\n\nRun the most relevant:\n```\n"
                    f"{r['pytest_command']}\n```"), calls

        # impact (default for a symbol)
        r = run("impact", symbol=s)
        if "error" in r:
            return r["error"], calls
        ex = run("experts", symbol=s)
        callers = ", ".join(f"`{T.short(c['qname'])}`" for c in r["callers"][:8])
        who = ", ".join(e["person"] for e in ex.get("experts", [])[:3])
        extra = ""
        if r["also_dispatches_from"]:
            extra = f"\n\nIt overrides {', '.join('`' + T.short(x) + '`' for x in r['also_dispatches_from'])}, " \
                    f"so callers of the base method count too."
        return (f"Changing `{T.short(r['target']['key'])}` is **{r['risk']} risk**: {r['affected_functions']} "
                f"production functions in {len(r['affected_modules'])} modules call into it"
                + (f" (nearest: {callers})" if callers else "") +
                f", and {r['test_count']} tests exercise it.{extra}\n\nAsk: {who or 'no clear owner'}"), calls

    # ---------------------------------------------------------------- llm
    def _ask_llm(self, question: str, mem_text: str, emit) -> tuple[str, list[dict]]:
        system = SYSTEM.format(repo=self.repo, memory=mem_text)
        if self.provider == "anthropic":
            return self._loop_anthropic(system, question, emit)
        return self._loop_openai(system, question, emit)

    def _run_tool_for_llm(self, name, args, calls, emit) -> str:
        emit({"type": "tool_start", "tool": name, "args": args})
        c = self.call_tool(name, args)
        calls.append(c)
        emit({"type": "tool_end", "tool": name, "ms": c["ms"], "cypher": c["cypher"]})
        return json.dumps(c["result"], default=str)[:12000]

    def _history(self, limit: int = 6) -> list[dict]:
        """This session's earlier turns as chat messages, so follow-ups like "yes, go ahead" make sense."""
        msgs = []
        for t in self.mem.recent_turns(self.session, limit):
            msgs.append({"role": "user", "content": t["question"]})
            msgs.append({"role": "assistant", "content": t["answer"][:3000]})
        return msgs

    def _loop_anthropic(self, system, question, emit):
        import anthropic

        client = anthropic.Anthropic(api_key=self.llm["api_key"])
        tools = [_schema(n, d, p, "anthropic") for n, d, p in SCHEMAS]
        msgs: list[dict[str, Any]] = self._history() + [{"role": "user", "content": question}]
        calls: list[dict] = []
        for _ in range(8):
            resp = client.messages.create(model=self.model, max_tokens=1500, system=system, tools=tools,
                                          messages=msgs)
            msgs.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                return "".join(b.text for b in resp.content if b.type == "text"), calls
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": u.id,
                 "content": self._run_tool_for_llm(u.name, dict(u.input), calls, emit)} for u in uses]})
        return "I ran out of steps before reaching an answer.", calls

    def _loop_openai(self, system, question, emit):
        from openai import OpenAI

        client = OpenAI(api_key=self.llm["api_key"], base_url=self.llm["base_url"])
        tools = [_schema(n, d, p, "openai") for n, d, p in SCHEMAS]
        msgs: list[dict[str, Any]] = ([{"role": "system", "content": system}] + self._history()
                                      + [{"role": "user", "content": question}])
        calls: list[dict] = []
        for _ in range(8):
            resp = client.chat.completions.create(model=self.model, messages=msgs, tools=tools,
                                                  max_tokens=int(os.environ.get("TRACEWISE_MAX_TOKENS", "1500")))
            m = resp.choices[0].message
            msgs.append(m.model_dump(exclude_none=True))
            if not m.tool_calls:
                return m.content or "", calls
            for tc in m.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                msgs.append({"role": "tool", "tool_call_id": tc.id,
                             "content": self._run_tool_for_llm(tc.function.name, args, calls, emit)})
        return "I ran out of steps before reaching an answer.", calls
