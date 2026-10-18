"""Background jobs started from the UI: clone a public git repository and index it.

Each job runs in a thread and keeps a short log the UI polls, so a user can add a
repository without touching a terminal. Only public https git URLs are accepted.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

REPOS_DIR = Path(os.environ.get("TRACEWISE_REPOS_DIR", "/data/repos" if Path("/data").is_dir() else "./data/repos"))
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?/[\w.\-/]+?(?:\.git)?/?$")
CLONE_TIMEOUT = int(os.environ.get("TRACEWISE_CLONE_TIMEOUT", "600"))
MAX_JOBS = 2  # indexing is CPU-heavy; keep a small cap

JOBS: dict[str, dict] = {}
_lock = threading.Lock()


def default_name(url: str) -> str:
    leaf = url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1].lower()
    leaf = re.sub(r"[^a-z0-9_-]", "-", leaf).strip("-")[:40]
    return leaf or "repo"


REV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")  # a tag, branch or sha; never starts with '-'


def validate(url: str, name: str, rev: str | None = None) -> str | None:
    if not URL_RE.match(url or ""):
        return "Use a public https git URL, e.g. https://github.com/pallets/click"
    if not NAME_RE.match(name or ""):
        return "Name: lowercase letters, digits, - or _, up to 40 characters"
    if rev and (not REV_RE.match(rev) or ".." in rev):
        return "Version: a tag, branch or commit such as 8.1.3 or main"
    return None


def start(url: str, name: str, rev: str | None = None) -> dict:
    url, name, rev = url.strip(), name.strip().lower(), (rev or "").strip() or None
    err = validate(url, name, rev)
    if err:
        return {"error": err}
    with _lock:
        running = [j for j in JOBS.values() if j["status"] == "running"]
        if any(j["name"] == name for j in running):
            return {"error": f"'{name}' is already being indexed"}
        if len(running) >= MAX_JOBS:
            return {"error": "Two repositories are already being indexed; try again when one finishes"}
        jid = uuid.uuid4().hex[:10]
        JOBS[jid] = {"id": jid, "url": url, "name": name, "rev": rev, "status": "running", "step": "queued",
                     "log": [], "started": time.time(), "finished": None, "stats": None, "error": None}
    threading.Thread(target=_run, args=(jid,), daemon=True).start()
    return {"job": JOBS[jid]}


def remove_clone(name: str) -> None:
    """Delete a repository's clone, but only one inside REPOS_DIR with a valid name."""
    if not NAME_RE.match(name or ""):
        return
    dest = (REPOS_DIR / name).resolve()
    if dest.parent == REPOS_DIR.resolve() and dest.is_dir():
        shutil.rmtree(dest, ignore_errors=True)


def get(jid: str) -> dict | None:
    return JOBS.get(jid)


def _log(job: dict, msg: str, step: str | None = None) -> None:
    job["log"].append(f"{time.strftime('%H:%M:%S')}  {msg}")
    job["log"] = job["log"][-60:]
    if step:
        job["step"] = step


def _run(jid: str) -> None:
    job = JOBS[jid]
    dest = REPOS_DIR / job["name"]
    try:
        REPOS_DIR.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            _log(job, f"updating existing clone in {dest}", "fetching")
            subprocess.run(["git", "-C", str(dest), "fetch", "--quiet", "--tags", "origin"], check=True,
                           capture_output=True, text=True, timeout=CLONE_TIMEOUT)
            subprocess.run(["git", "-C", str(dest), "reset", "--quiet", "--hard", "FETCH_HEAD"], check=True,
                           capture_output=True, text=True, timeout=120)
        else:
            _log(job, f"cloning {job['url']} (full history, needed for blame and PRs)", "cloning")
            r = subprocess.run(["git", "clone", "--quiet", job["url"], str(dest)], capture_output=True, text=True,
                               timeout=CLONE_TIMEOUT, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            if r.returncode != 0:
                raise RuntimeError("git clone failed: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1]
                                   + " (is the repository public?)")
        n = subprocess.run(["git", "-C", str(dest), "rev-list", "--count", "HEAD"], capture_output=True,
                           text=True).stdout.strip()
        _log(job, f"clone ready: {n} commits", "indexing")

        from .ingest import ingest

        def log(msg):
            _log(job, msg, "indexing")

        stats = ingest(dest, job["name"], rev=job["rev"], log=log)
        if not stats.get("code", {}).get("functions"):
            _log(job, "warning: no Python functions found; history and people are still indexed")
        job["stats"] = stats
        _log(job, f"done: '{job['name']}' is ready to ask about", "done")
        job["status"] = "done"
    except subprocess.TimeoutExpired:
        job["status"], job["error"] = "failed", f"timed out after {CLONE_TIMEOUT}s (repository too large?)"
        _log(job, job["error"], "failed")
    except Exception as exc:  # report, don't crash the server
        job["status"], job["error"] = "failed", str(exc)[:500]
        _log(job, "failed: " + job["error"], "failed")
        if dest.exists() and job["step"] == "cloning":
            shutil.rmtree(dest, ignore_errors=True)
    finally:
        job["finished"] = time.time()
