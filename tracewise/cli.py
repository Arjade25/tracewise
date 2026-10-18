"""tracewise CLI.

    tracewise index ./path/to/repo --name flask [--coverage-python ./venv/bin/python]
    tracewise ask flask "what breaks if I change full_dispatch_request?"
    tracewise review flask --symbols url_for Flask.make_response
    tracewise serve --port 8000
    tracewise mcp --repo flask
    tracewise db                      # local FalkorDB without Docker
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tracewise")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="build the graph for a local git repository")
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--max-commits", type=int)
    p.add_argument("--coverage-python", help="python with pytest+coverage installed for the target repo; "
                                               "runs its tests to record which tests execute which functions")
    p.add_argument("--keep", action="store_true", help="update the existing graph instead of rebuilding it")
    p.add_argument("--rev", help="index the repository as of this git revision (tag, branch or sha)")

    p = sub.add_parser("ask", help="ask the agent a question")
    p.add_argument("repo")
    p.add_argument("question")
    p.add_argument("--user", default="cli")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("review", help="multi-agent review of a change")
    p.add_argument("repo")
    p.add_argument("--symbols", nargs="*", default=[])
    p.add_argument("--diff", help="path to a unified diff file, or - for stdin")
    p.add_argument("--user", default="cli")

    p = sub.add_parser("serve", help="run the web UI + HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true", help="restart automatically when code changes (development)")

    p = sub.add_parser("mcp", help="run the MCP server (stdio)")
    p.add_argument("--repo", default="flask")
    p.add_argument("--user", default="mcp")
    p.add_argument("--transport", default="stdio")

    sub.add_parser("repos", help="list indexed repositories")

    p = sub.add_parser("db", help="run a local FalkorDB (no Docker; needs the falkordblite package)")
    p.add_argument("--port", type=int, default=6379)
    p.add_argument("--dir", default="./data")

    a = ap.parse_args(argv)
    if a.cmd == "index":
        from .ingest import ingest
        ingest(a.path, a.name, reset=not a.keep, rev=a.rev, max_commits=a.max_commits,
               coverage_python=a.coverage_python)
    elif a.cmd == "ask":
        from .agent import Agent
        r = Agent(a.repo, user=a.user).ask(a.question)
        if a.json:
            print(json.dumps(r, default=str, indent=1))
        else:
            print(r["answer"])
            print(f"\n— {r['mode']} · tools: {', '.join(c['tool'] for c in r['tool_calls'])} · "
                  f"{sum(len(c['cypher']) for c in r['tool_calls'])} Cypher queries · {r['ms']} ms")
    elif a.cmd == "review":
        from .crew import review
        diff = None
        if a.diff:
            diff = sys.stdin.read() if a.diff == "-" else open(a.diff, encoding="utf-8").read()
        r = review(a.repo, a.user, symbols=a.symbols, diff=diff)
        print(r.get("summary") or r.get("error"))
    elif a.cmd == "serve":
        import uvicorn
        from pathlib import Path
        uvicorn.run("tracewise.api:app", host=a.host, port=a.port, reload=a.reload,
                    reload_dirs=[str(Path(__file__).parent)] if a.reload else None)
    elif a.cmd == "mcp":
        from . import mcp_server
        sys.argv = ["tracewise-mcp", "--repo", a.repo, "--user", a.user, "--transport", a.transport]
        mcp_server.main()
    elif a.cmd == "db":
        from .localdb import start
        print(f"FalkorDB on localhost:{a.port}, data in {a.dir}")
        start(a.port, a.dir, foreground=True)
    elif a.cmd == "repos":
        from .db import list_repos
        print("\n".join(list_repos()) or "(none)")


if __name__ == "__main__":
    main()
