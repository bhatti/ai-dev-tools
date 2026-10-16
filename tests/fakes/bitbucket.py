"""A local HTTP server that speaks enough of the Bitbucket Cloud REST API for the PR-metrics code.

``with FakeBitbucket(monkeypatch) as bb:`` sets BITBUCKET_API_BASE. ``bb.add(path_suffix, json_body)`` registers a
GET answer (matched on the request path, query ignored); ``bb.add_pages(path, [page1, page2])`` serves a
paginated list with ``next`` links; ``bb.fail(path, status, times, headers)`` makes the next calls fail first.
``bb.requests`` is the ordered list of requested paths.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class FakeBitbucket:
    def __init__(self, monkeypatch):
        self.routes: dict[str, list] = {}
        self.failures: dict[str, list] = {}
        self.requests: list[str] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                with outer._lock:
                    outer.requests.append(path + (("?" + parsed.query) if parsed.query else ""))
                    fails = outer.failures.get(path)
                    fail = fails.pop(0) if fails else None
                    pages = outer.routes.get(path)
                if fail:
                    status, headers = fail
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if isinstance(pages, str):   # a raw (non-JSON) body, e.g. a proxy error page
                    data = pages.encode()
                    self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(data)))
                    self.end_headers(); self.wfile.write(data)
                    return
                if pages is None:
                    self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
                page = 0
                for part in parsed.query.split("&"):
                    if part.startswith("page="):
                        page = int(part[5:]) - 1
                body = dict(pages[page]) if isinstance(pages, list) and page < len(pages) else {}
                if isinstance(pages, list) and len(pages) > 1 and page + 1 < len(pages):
                    kept = "&".join(p for p in parsed.query.split("&") if p and not p.startswith("page="))   # real `next` links carry the query
                    body["next"] = f"http://127.0.0.1:{outer.server.server_port}{path}?{kept + '&' if kept else ''}page={page + 2}"
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}/2.0"
        monkeypatch.setenv("BITBUCKET_API_BASE", self.base)

    def add(self, path: str, body: dict) -> "FakeBitbucket":
        self.routes[f"/2.0{path}"] = [body]
        return self

    def add_raw(self, path: str, body: str) -> "FakeBitbucket":
        self.routes[f"/2.0{path}"] = body
        return self

    def query(self, suffix: str) -> str:
        """Query string of the last request whose path ends with ``suffix``."""
        for r in reversed(self.requests):
            if r.split("?")[0].endswith(suffix):
                return r.partition("?")[2]
        return ""

    def add_pages(self, path: str, pages: list[dict]) -> "FakeBitbucket":
        self.routes[f"/2.0{path}"] = pages
        return self

    def fail(self, path: str, status: int, times: int = 1, headers: dict | None = None) -> "FakeBitbucket":
        self.failures.setdefault(f"/2.0{path}", []).extend([(status, headers or {})] * times)
        return self

    def count(self, suffix: str) -> int:
        return sum(1 for r in self.requests if r.split("?")[0].endswith(suffix))

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
