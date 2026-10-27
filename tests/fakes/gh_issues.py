"""A stateful `gh` executable for issues: `issue view|list|edit --body-file|comment`, plus a canned `api graphql` answer.

``FakeGhIssues(tmp_path, monkeypatch)`` installs it on PATH. ``add_issue(number, title, body, labels=[...], ...)`` registers
issues; edits made through the CLI change them, so a test reads ``body(n)`` back like production code does. ``calls`` is
the argv log. ``graphql`` (set by the test) is the JSON returned for `gh api graphql`.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

SCRIPT = r'''#!/usr/bin/env python3
import json, sys, pathlib
base = pathlib.Path(__file__).parent
state_path = base / "state.json"
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with open(base / "calls.jsonl", "a") as f:
    f.write(json.dumps(args) + "\n")

def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default

def project(issue, fields, cap=None):
    out = {k: v for k, v in issue.items() if k in fields}
    if cap is not None and "comments" in out:
        out["comments"] = out["comments"][:cap]   # `gh issue list` returns at most 100 comments per issue
    return out

if state.get("transient", 0) > 0:
    state["transient"] -= 1
    state_path.write_text(json.dumps(state))
    sys.stderr.write(state.get("transient_message", "API rate limit exceeded for user") + "\n"); sys.exit(1)
if state.get("garbage"):
    sys.stdout.write("<html>not json</html>"); sys.exit(0)

if args[:2] == ["api", "user"]:
    if state.get("whoami_fail"):
        sys.stderr.write(state["whoami_fail"] + "\n"); sys.exit(1)
    sys.stdout.write(state.get("whoami", "someone") + "\n"); sys.exit(0)
if args[:2] == ["api", "graphql"]:
    if state.get("graphql_fail"):
        sys.stderr.write(state["graphql_fail"]); sys.exit(1)
    sys.stdout.write(json.dumps(state.get("graphql", {}))); sys.exit(0)
if args[:1] != ["issue"]:
    sys.stderr.write("fake gh: unsupported: " + " ".join(args) + "\n"); sys.exit(1)
verb = args[1]
fields = (opt("--json") or "").split(",")
if verb == "view":
    number = args[2]
    issue = state["issues"].get(number)
    if issue is None:
        sys.stderr.write("GraphQL: Could not resolve to an issue or pull request with the number of " + number + "\n"); sys.exit(1)
    sys.stdout.write(json.dumps(project(issue, fields)))
elif verb == "list":
    wanted = state["issues"].values()
    if opt("--label"):
        wanted = [i for i in wanted if opt("--label") in [l["name"] for l in i["labels"]]]
    if opt("--milestone"):
        wanted = [i for i in wanted if (i.get("milestone") or {}).get("title") == opt("--milestone")]
    if opt("--state", "open") != "all":
        wanted = [i for i in wanted if i["state"].lower() == opt("--state", "open")]
    sys.stdout.write(json.dumps([project(i, fields, cap=100) for i in list(wanted)[: int(opt("--limit", "30"))]]))
elif verb == "edit":
    issue = state["issues"][args[2]]
    if state.get("edit_fail"):
        sys.stderr.write(state["edit_fail"]); sys.exit(1)
    if "--body-file" in args:
        issue["body"] = pathlib.Path(opt("--body-file")).read_text()
    state_path.write_text(json.dumps(state))
elif verb == "comment":
    issue = state["issues"][args[2]]
    issue["comments"].append({"author": {"login": "bot"}, "body": opt("--body")})
    state_path.write_text(json.dumps(state))
else:
    sys.stderr.write("fake gh: unsupported issue verb " + verb + "\n"); sys.exit(1)
'''


class FakeGhIssues:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.dir = tmp_path / "fake-gh-issues"
        self.dir.mkdir()
        exe = self.dir / "gh"
        exe.write_text(SCRIPT)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{self.dir}:/usr/bin:/bin")
        self._state = {"issues": {}}
        self._save()

    def _save(self) -> None:
        (self.dir / "state.json").write_text(json.dumps(self._state))

    def _load(self) -> dict:
        return json.loads((self.dir / "state.json").read_text())

    def add_issue(self, number: int, title: str, body: str = "", labels: list[str] | None = None, milestone: str | None = None,
                  state: str = "OPEN", assignees: list[str] | None = None, comments: list[str] | None = None) -> "FakeGhIssues":
        self._state = self._load()
        self._state["issues"][str(number)] = {
            "number": number, "title": title, "body": body, "state": state, "url": f"https://github.com/acme/widgets/issues/{number}",
            "labels": [{"name": n} for n in labels or []], "assignees": [{"login": a} for a in assignees or []],
            "milestone": {"title": milestone} if milestone else None,
            "comments": [{"author": {"login": "someone"}, "body": c} for c in comments or []]}
        self._save()
        return self

    def set(self, key: str, value) -> "FakeGhIssues":
        self._state = self._load()
        self._state[key] = value
        self._save()
        return self

    def body(self, number: int) -> str:
        return self._load()["issues"][str(number)]["body"]

    def comments(self, number: int) -> list[str]:
        return [c["body"] for c in self._load()["issues"][str(number)]["comments"]]

    @property
    def calls(self) -> list[list[str]]:
        path = self.dir / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def calls_matching(self, needle: str) -> list[list[str]]:
        return [c for c in self.calls if needle in " ".join(c)]
