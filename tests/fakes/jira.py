"""A stateful local HTTP server that speaks the part of the Jira Cloud REST API v3 the ticket tools use.

``with FakeJira(monkeypatch) as jira:`` sets JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN. Issues live in ``jira.issues``
and PUT/POST calls change them, so a test can read the description back after a write exactly like production code does.

* ``add_issue(key, title, description=<markdown str | ADF dict | None>, points=3, ...)`` registers an issue
* ``jira.page_size`` controls search pagination (``nextPageToken``); ``add_search(jql_fragment, [keys])`` selects the
  result of a search whose JQL contains the fragment (default: every issue)
* ``fail(path_suffix, status, times, headers, method=None)`` makes the next matching calls fail first; ``requests`` is the ordered request log
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from scripts.common import adf


class FakeJira:
    def __init__(self, monkeypatch):
        self.issues: dict[str, dict] = {}
        self.fields: list[dict] = [{"id": "customfield_10016", "name": "Story point estimate"},
                                   {"id": "customfield_10020", "name": "Sprint"}]
        self.searches: list[tuple[str, list[str]]] = []
        self.failures: dict[str, list] = {}
        self.requests: list[tuple[str, str, object]] = []
        self.page_size = 100
        self.embedded_comment_cap = 1000  # real Jira embeds only a first page of comments in the issue payload
        self.comment_page_size = 100
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status: int, body: object | None = None, headers: dict | None = None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str):
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                with outer._lock:
                    outer.requests.append((method, parsed.path, body))
                    for suffix, queue in outer.failures.items():
                        if parsed.path.endswith(suffix) and queue and queue[0][2] in (None, method):
                            status, headers, _ = queue.pop(0)
                            return self._send(status, {"errorMessages": ["injected failure"]}, headers)
                    status, payload = outer._route(method, parsed.path, parse_qs(parsed.query), body)
                self._send(status, payload)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        monkeypatch.setenv("JIRA_BASE_URL", self.base)
        monkeypatch.setenv("JIRA_EMAIL", "tester@example.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "test-token-123")

    # -- data -------------------------------------------------------------------------------------------------
    def add_issue(self, key: str, title: str, description: object = None, points: float | None = None, status: str = "To Do",
                  category: str = "new", issue_type: str = "Story", assignee: str | None = "Alex Doe", labels: list[str] | None = None,
                  parent: str | None = None, links: list[dict] | None = None, comments: list[str] | None = None,
                  points_field: str = "customfield_10016") -> "FakeJira":
        if isinstance(description, str):
            description = {"type": "doc", "version": 1, "content": adf.from_markdown(description)}
        fields = {"summary": title, "description": description, "labels": labels or [], "assignee": {"displayName": assignee} if assignee else None,
                  "status": {"name": status, "statusCategory": {"key": category}},
                  "issuetype": {"name": issue_type, "subtask": issue_type.lower() in ("sub-task", "subtask")},
                  "parent": {"key": parent} if parent else None, "issuelinks": links or [],
                  "comment": {"comments": [{"body": {"type": "doc", "version": 1, "content": adf.from_markdown(c)}} for c in comments or []]},
                  points_field: points}
        self.issues[key] = {"key": key, "fields": fields}
        return self

    def add_field(self, field_id: str, name: str) -> "FakeJira":
        self.fields.append({"id": field_id, "name": name})
        return self

    def add_search(self, jql_fragment: str, keys: list[str]) -> "FakeJira":
        self.searches.append((jql_fragment, keys))
        return self

    def fail(self, path_suffix: str, status: int, times: int = 1, headers: dict | None = None, method: str | None = None) -> "FakeJira":
        self.failures.setdefault(path_suffix, []).extend([(status, headers or {}, method)] * times)
        return self

    def description_text(self, key: str) -> str:
        return adf.to_text(self.issues[key]["fields"]["description"])

    def writes(self) -> list[tuple[str, str, object]]:
        return [r for r in self.requests if r[0] in ("PUT", "POST") and not r[1].endswith("/search/jql")]

    # -- routing ----------------------------------------------------------------------------------------------
    def _issue_view(self, issue: dict, wanted: list[str] | None) -> dict:
        fields = dict(issue["fields"] if not wanted else {k: v for k, v in issue["fields"].items() if k in wanted})
        if "comment" in fields:
            all_comments = issue["fields"]["comment"]["comments"]
            fields["comment"] = {"comments": all_comments[:self.embedded_comment_cap], "total": len(all_comments),
                                 "maxResults": self.embedded_comment_cap, "startAt": 0}
        return {"key": issue["key"], "fields": fields}

    def _route(self, method: str, path: str, query: dict, body: object):
        if method == "GET" and path == "/rest/api/3/field":
            return 200, self.fields
        if method == "POST" and path == "/rest/api/3/search/jql":
            return self._search(body or {})
        match = re.fullmatch(r"/rest/api/3/issue/([A-Z][A-Z0-9]+-\d+)(/comment)?", path)
        if not match:
            return 404, {"errorMessages": ["not found"]}
        key, comment = match.group(1), match.group(2)
        issue = self.issues.get(key)
        if issue is None:
            return 404, {"errorMessages": ["Issue does not exist or you do not have permission to see it."]}
        if method == "GET" and not comment:
            wanted = (query.get("fields") or [""])[0].split(",") if query.get("fields") else None
            return 200, self._issue_view(issue, wanted)
        if method == "PUT" and not comment:
            issue["fields"].update((body or {}).get("fields", {}))
            return 204, None
        if method == "GET" and comment:
            start = int((query.get("startAt") or ["0"])[0])
            all_comments = issue["fields"]["comment"]["comments"]
            return 200, {"comments": all_comments[start:start + self.comment_page_size], "total": len(all_comments), "startAt": start,
                         "maxResults": self.comment_page_size}
        if method == "POST" and comment:
            issue["fields"]["comment"]["comments"].append({"body": (body or {}).get("body")})
            return 201, {"id": "1"}
        return 405, {"errorMessages": ["method not allowed"]}

    def _search(self, body: dict):
        jql = body.get("jql", "")
        keys = next((k for fragment, k in self.searches if fragment in jql), list(self.issues))
        start = int(body.get("nextPageToken") or 0)
        page = keys[start:start + self.page_size]
        wanted = body.get("fields")
        out = {"issues": [self._issue_view(self.issues[k], wanted) for k in page if k in self.issues]}
        if start + self.page_size < len(keys):
            out["nextPageToken"] = str(start + self.page_size)
        return 200, out

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
