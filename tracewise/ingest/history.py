"""History + decision layer from git and the project changelog.

Nodes:  Person, Commit, PR, Issue, Release, ChangeNote
Edges:  (Person)-[:AUTHORED]->(Commit)
        (Commit)-[:MODIFIED {added, deleted}]->(Module)
        (Commit)-[:CHANGED]->(Function)          # commit owns surviving lines (git blame)
        (Person)-[:WROTE {lines}]->(Function)     # surviving lines per author (git blame)
        (Commit)-[:PART_OF]->(PR)
        (PR|Commit)-[:FIXES]->(Issue)
        (Release)-[:HAS_NOTE]->(ChangeNote)-[:REFERENCES]->(PR|Issue)
        (ChangeNote)-[:MENTIONS]->(Function|Class|Module)

Everything comes from the local git clone and files inside it: no private data,
no API keys needed.
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from .code import CodeIndex

FIX_RE = re.compile(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s*:?\s+((?:#\d+[\s,]*(?:and\s+)?)+)", re.I)
NUM_RE = re.compile(r"#(\d+)")
SQUASH_RE = re.compile(r"^(.*)\s+\(#(\d+)\)\s*$")
MERGE_RE = re.compile(r"^Merge pull request #(\d+) from ([^\s]+)")
ROLE_RE = re.compile(r":(issue|pr|func|meth|class|mod|attr|data):`~?!?([^`<]+?)(?:\s*<([^>]+)>)?`")
# MyST (Markdown) roles: {pr}`123`, {meth}`Option.get_help_spec`
MYST_RE = re.compile(r"\{(issue|pr|func|meth|class|mod|attr|data)\}`~?!?([^`<]+?)(?:\s*<([^>]+)>)?`")
CHANGELOG_FILES = ["CHANGES.rst", "CHANGES.md", "CHANGELOG.md", "CHANGELOG.rst", "HISTORY.md", "HISTORY.rst",
                   "NEWS.md", "NEWS.rst", "docs/changelog.md", "docs/changelog.rst", "docs/changes.rst"]
# "Version 3.0.0", "## Version 8.6.0", "## [1.2.0] - 2024-01-01", "## v2.1 (2023-05-01)", "1.4.0 (2022-02-01)"
VERSION_RE = re.compile(r"^(?:#{1,3}\s+)?(?:Version\s+|Release\s+|\[)?v?(\d+\.\d+(?:\.\d+)?(?:[\w.\-+]*))\]?"
                        r"(?:\s*[-–(]\s*(\d{4}-\d{2}-\d{2})\)?)?\s*$", re.I)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
    ).stdout


def _norm_path(p: str) -> str:
    # numstat renames: "src/{old => new}/x.py" or "old.py => new.py"
    if "=>" not in p:
        return p
    m = re.match(r"^(.*)\{(.*) => (.*)\}(.*)$", p)
    if m:
        return (m.group(1) + m.group(3) + m.group(4)).replace("//", "/")
    return p.split("=>")[-1].strip()


def parse_log(repo: Path, max_commits: int | None = None) -> list[dict]:
    fmt = "%x1e%H%x1f%aN%x1f%aE%x1f%at%x1f%P%x1f%s%x1f%b%x1d"
    args = ["log", "-M", "--numstat", f"--format={fmt}"]
    if max_commits:
        args.append(f"-n{max_commits}")
    out = git(repo, *args)
    commits = []
    for chunk in out.split("\x1e")[1:]:
        head, _, stats = chunk.partition("\x1d")
        sha, name, email, ts, parents, subject, body = head.split("\x1f")
        files = []
        for line in stats.strip().splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            a, d, p = parts
            files.append({
                "path": _norm_path(p), "added": int(a) if a.isdigit() else 0,
                "deleted": int(d) if d.isdigit() else 0,
            })
        commits.append({
            "sha": sha, "author": name.strip(), "email": email.strip().lower(), "ts": int(ts),
            "parents": parents.split(), "subject": subject.strip(), "body": body.strip(), "files": files,
        })
    return commits


def extract_prs(repo: Path, commits: list[dict]) -> tuple[dict, dict]:
    """Return ({pr_number: {title, branch}}, {sha: pr_number})."""
    prs: dict[int, dict] = {}
    membership: dict[str, int] = {}
    for c in commits:
        m = MERGE_RE.match(c["subject"])
        if m and len(c["parents"]) == 2:
            n = int(m.group(1))
            title = (c["body"].splitlines() or [""])[0].strip() or m.group(2)
            prs[n] = {"title": title, "branch": m.group(2), "merged_at": c["ts"], "merge_sha": c["sha"]}
            membership[c["sha"]] = n
            try:
                inner = git(repo, "rev-list", f"{c['parents'][0]}..{c['parents'][1]}").split()
            except subprocess.CalledProcessError:
                inner = []
            for s in inner:
                membership.setdefault(s, n)
            continue
        m = SQUASH_RE.match(c["subject"])
        if m:
            n = int(m.group(2))
            prs.setdefault(n, {"title": m.group(1).strip(), "branch": None, "merged_at": c["ts"],
                               "merge_sha": c["sha"]})
            membership[c["sha"]] = n
            if len(c["parents"]) == 2:  # merge commit with a custom "title (#N)" subject
                try:
                    inner = git(repo, "rev-list", f"{c['parents'][0]}..{c['parents'][1]}").split()
                except subprocess.CalledProcessError:
                    inner = []
                for s in inner:
                    membership.setdefault(s, n)
    return prs, membership


def extract_fixes(text: str) -> set[int]:
    out = set()
    for m in FIX_RE.finditer(text):
        out.update(int(x) for x in NUM_RE.findall(m.group(1)))
    return out


def find_changelog(repo: Path) -> Path | None:
    for name in CHANGELOG_FILES:
        if (repo / name).is_file():
            return repo / name
    return None


def parse_changelog(path: Path | None) -> list[dict]:
    """Parse a changelog into releases with bullet notes.

    Handles reStructuredText (``Version X`` underlined with dashes, the Pallets style) and
    Markdown (``## Version X``, ``## [X] - date``, ``## vX (date)``), with bullets that wrap
    onto indented continuation lines and an optional ``Released YYYY-MM-DD`` line.
    """
    if not path or not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    md = path.suffix.lower() == ".md"
    releases, cur, note = [], None, None

    def flush():
        nonlocal note
        if note and cur:
            cur["notes"].append(note)
        note = None

    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        is_heading = (md and line.startswith("#")) or (not md and nxt.strip() and set(nxt.strip()) <= set("-=~^"))
        m = VERSION_RE.match(line.strip()) if is_heading or (md and line.startswith("#")) else None
        if m:
            flush()
            cur = {"version": m.group(1), "date": m.group(2), "notes": []}
            releases.append(cur)
            i += 1 if md else 2
            continue
        if cur is not None:
            stripped = line.strip()
            dm = re.match(r"^(?:Released|Release date:?)\s+(\d{4}-\d{2}-\d{2})", stripped, re.I)
            if dm:
                cur["date"] = dm.group(1)
            elif re.match(r"^[-*]\s+", line):
                flush()
                note = re.sub(r"^[-*]\s+", "", line).strip()
            elif note is not None and line.startswith(("  ", "\t")) and stripped:
                note += " " + stripped
            elif note is not None and stripped:
                flush()
        i += 1
    flush()
    return [r for r in releases if r["notes"] or r["date"]]


def blame_functions(repo: Path, index: CodeIndex) -> tuple[list[dict], list[dict]]:
    """git blame each indexed file at HEAD; attribute surviving lines to functions."""
    by_path: dict[str, list] = defaultdict(list)
    for f in index.funcs.values():
        by_path[f.path].append(f)
    wrote, changed = [], []
    for path, funcs in by_path.items():
        try:
            out = git(repo, "blame", "--line-porcelain", "-w", "HEAD", "--", path)
        except subprocess.CalledProcessError:
            continue
        line_info: dict[int, tuple[str, str]] = {}
        sha = author = None
        final_line = 0
        for raw in out.splitlines():
            if raw.startswith("\t"):
                line_info[final_line] = (sha, author)
                continue
            parts = raw.split(" ")
            if len(parts) >= 3 and len(parts[0]) == 40 and all(ch in "0123456789abcdef" for ch in parts[0]):
                sha, final_line = parts[0], int(parts[2])
            elif raw.startswith("author "):
                author = raw[7:].strip()
        for f in funcs:
            authors, shas = Counter(), Counter()
            for ln in range(f.line, f.end_line + 1):
                if ln in line_info:
                    s, a = line_info[ln]
                    authors[a] += 1
                    shas[s] += 1
            for a, n in authors.items():
                wrote.append({"f": f.qname, "a": a, "n": n})
            for s, n in shas.items():
                changed.append({"f": f.qname, "s": s, "n": n})
    return wrote, changed


def load_history(g, repo: Path, index: CodeIndex, max_commits: int | None = None) -> dict:
    from ..db import batched

    commits = parse_log(repo, max_commits)
    prs, membership = extract_prs(repo, commits)
    py_paths = {m.path for m in index.modules.values()}

    # People: keyed by mailmapped display name; emails kept as a list.
    people: dict[str, set] = defaultdict(set)
    for c in commits:
        people[c["author"]].add(c["email"])
    for b in batched([{"name": n, "emails": sorted(e)} for n, e in people.items()]):
        g.query(
            """UNWIND $rows AS r MERGE (p:Person {name: r.name})
               SET p.emails = r.emails, p.email = r.emails[0]""",
            {"rows": b},
        )

    crow = [{
        "sha": c["sha"], "short": c["sha"][:8], "author": c["author"], "ts": c["ts"],
        "subject": c["subject"][:300], "body": c["body"][:1500], "is_merge": len(c["parents"]) > 1,
        "files_changed": len(c["files"]),
    } for c in commits]
    for b in batched(crow, 1000):
        g.query(
            """UNWIND $rows AS r
               MERGE (c:Commit {sha: r.sha})
               SET c.short = r.short, c.ts = r.ts, c.subject = r.subject, c.body = r.body,
                   c.is_merge = r.is_merge, c.files_changed = r.files_changed
               WITH c, r MATCH (p:Person {name: r.author}) MERGE (p)-[:AUTHORED]->(c)""",
            {"rows": b},
        )

    mods = [{"sha": c["sha"], "path": f["path"], "a": f["added"], "d": f["deleted"]}
            for c in commits for f in c["files"] if f["path"].endswith(".py")]
    hist_only = sorted({m["path"] for m in mods} - py_paths)
    for b in batched([{"path": p} for p in hist_only]):
        g.query(
            """UNWIND $rows AS r MERGE (m:Module {path: r.path})
               ON CREATE SET m.exists = false, m.name = r.path""",
            {"rows": b},
        )
    for b in batched(mods, 2000):
        g.query(
            """UNWIND $rows AS r
               MATCH (c:Commit {sha: r.sha}), (m:Module {path: r.path})
               MERGE (c)-[e:MODIFIED]->(m) SET e.added = r.a, e.deleted = r.d""",
            {"rows": b},
        )

    for b in batched([{"n": n, **v} for n, v in prs.items()]):
        g.query(
            """UNWIND $rows AS r MERGE (p:PR {number: r.n})
               SET p.title = r.title, p.branch = r.branch, p.merged_at = r.merged_at,
                   p.merge_sha = r.merge_sha""",
            {"rows": b},
        )
    for b in batched([{"s": s, "n": n} for s, n in membership.items()], 2000):
        g.query(
            """UNWIND $rows AS r MATCH (c:Commit {sha: r.s}), (p:PR {number: r.n})
               MERGE (c)-[:PART_OF]->(p)""",
            {"rows": b},
        )

    fixes = []
    for c in commits:
        for n in extract_fixes(c["subject"] + "\n" + c["body"]):
            if n in prs:  # "#123" pointing at a PR, not an issue
                continue
            pr = membership.get(c["sha"])
            fixes.append({"src": "PR" if pr else "Commit", "pr": pr, "sha": c["sha"], "issue": n})
    for b in batched(fixes):
        g.query(
            """UNWIND $rows AS r MERGE (i:Issue {number: r.issue})
               WITH i, r
               OPTIONAL MATCH (p:PR {number: r.pr})
               OPTIONAL MATCH (c:Commit {sha: r.sha})
               FOREACH (_ IN CASE WHEN p IS NOT NULL THEN [1] ELSE [] END | MERGE (p)-[:FIXES]->(i))
               FOREACH (_ IN CASE WHEN p IS NULL THEN [1] ELSE [] END | MERGE (c)-[:FIXES]->(i))""",
            {"rows": b},
        )

    wrote, changed = blame_functions(repo, index)
    for b in batched(wrote, 2000):
        g.query(
            """UNWIND $rows AS r MATCH (f:Function {qname: r.f}), (p:Person {name: r.a})
               MERGE (p)-[e:WROTE]->(f) SET e.lines = r.n""",
            {"rows": b},
        )
    for b in batched(changed, 2000):
        g.query(
            """UNWIND $rows AS r MATCH (f:Function {qname: r.f}), (c:Commit {sha: r.s})
               MERGE (c)-[e:CHANGED]->(f) SET e.lines = r.n""",
            {"rows": b},
        )

    # Changelog: releases and the intent behind each change.
    releases = parse_changelog(find_changelog(repo))
    names = defaultdict(set)
    for q in list(index.funcs) + list(index.classes) + list(index.modules):
        names[q.rsplit(".", 1)[-1]].add(q)
        names[q].add(q)
    note_rows, ref_rows, men_rows = [], [], []
    for r in releases:
        for k, text in enumerate(r["notes"]):
            nid = f"{r['version']}#{k}"
            clean = ROLE_RE.sub(lambda m: (m.group(3) or m.group(2)).strip(), text)
            clean = MYST_RE.sub(lambda m: ("#" if m.group(1) in ("pr", "issue") else "") + (m.group(3) or m.group(2)).strip(),
                                clean).replace("``", "`")
            note_rows.append({"id": nid, "v": r["version"], "date": r["date"], "text": clean[:1000]})
            found = ROLE_RE.findall(text) + MYST_RE.findall(text) + [
                ("lit", t, "") for t in re.findall(r"``([A-Za-z_][\w.]*)(?:\(\))?``", text)
            ] + [
                # Markdown single-backtick code that looks like a qualified or snake_case name
                ("lit", t, "") for t in re.findall(r"(?<!`)`([A-Za-z_]\w*(?:\.\w+)+|[a-z]+_[a-z_]+)(?:\(\))?`(?!`)", text)
            ] + [
                # plain "#123" / "(#123)" references, common in Markdown changelogs
                ("pr", n, "") for n in re.findall(r"(?<![\w&])#(\d{2,6})\b", text)
            ]
            for role, target, alt in found:
                target = (alt or target).strip()
                if role in ("issue", "pr"):
                    for num in re.findall(r"\d+", target):
                        ref_rows.append({"id": nid, "kind": role, "n": int(num)})
                else:
                    t = target.strip().rstrip("()").lstrip("~.")
                    cands = names.get(t) or names.get(t.rsplit(".", 1)[-1]) or set()
                    cands = {c for c in cands if not c.startswith(("tests", "examples"))}
                    if 1 <= len(cands) <= 3:
                        for c in cands:
                            men_rows.append({"id": nid, "q": c})
    for b in batched([{"v": r["version"], "date": r["date"]} for r in releases]):
        g.query("UNWIND $rows AS r MERGE (x:Release {version: r.v}) SET x.date = r.date", {"rows": b})
    for b in batched(note_rows):
        g.query(
            """UNWIND $rows AS r MATCH (x:Release {version: r.v})
               MERGE (n:ChangeNote {id: r.id}) SET n.text = r.text, n.version = r.v, n.date = r.date
               MERGE (x)-[:HAS_NOTE]->(n)""",
            {"rows": b},
        )
    for b in batched(ref_rows):
        g.query(
            """UNWIND $rows AS r MATCH (n:ChangeNote {id: r.id})
               FOREACH (_ IN CASE WHEN r.kind = 'pr' THEN [1] ELSE [] END |
                   MERGE (p:PR {number: r.n}) MERGE (n)-[:REFERENCES]->(p))
               FOREACH (_ IN CASE WHEN r.kind = 'issue' THEN [1] ELSE [] END |
                   MERGE (i:Issue {number: r.n}) MERGE (n)-[:REFERENCES]->(i))""",
            {"rows": b},
        )
    # A note citing both a PR and an issue tells us the PR fixed the issue.
    g.query(
        """MATCH (p:PR)<-[:REFERENCES]-(n:ChangeNote)-[:REFERENCES]->(i:Issue)
           MERGE (p)-[:FIXES]->(i)"""
    )
    for b in batched(men_rows):
        g.query(
            """UNWIND $rows AS r MATCH (n:ChangeNote {id: r.id})
               OPTIONAL MATCH (f:Function {qname: r.q})
               OPTIONAL MATCH (c:Class {qname: r.q})
               OPTIONAL MATCH (m:Module {name: r.q})
               WITH n, coalesce(f, c, m) AS t WHERE t IS NOT NULL
               MERGE (n)-[:MENTIONS]->(t)""",
            {"rows": b},
        )
    return {
        "commits": len(commits), "people": len(people), "prs": len(prs),
        "pr_commit_links": len(membership), "fix_links": len(fixes), "wrote": len(wrote),
        "changed": len(changed), "releases": len(releases), "notes": len(note_rows),
        "note_refs": len(ref_rows), "mentions": len(men_rows),
    }
