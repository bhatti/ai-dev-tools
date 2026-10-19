"""Tests for scripts/qa/helpers.sh against a throwaway local HTTP app (needs bash, curl, jq)."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not all(shutil.which(t) for t in ("bash", "curl", "jq")), reason="bash/curl/jq required")

HELPERS = Path(__file__).resolve().parents[1] / "scripts" / "qa" / "helpers.sh"
SEEN: list[tuple[str, str, str | None]] = []
LIMITED: list[int] = []


class App(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        SEEN.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path == "/truncated":  # promises 100 bytes, sends 8, closes: curl exit 18
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b'{"ok":tr')
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path == "/limited-once":  # rate limited on the first request only
            first = not LIMITED
            LIMITED.append(1)
            if first:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            else:
                self._send(200, {"ok": True})
            return
        if self.path == "/limited-always":
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if self.path == "/secure":
            self._send(200 if self.headers.get("Authorization") else 401, {"secure": True})
            return
        if self.path == "/items":
            self._send(200, {"items": [1, 2], "token": "visible-in-body"})
        elif self.path == "/boom":
            self._send(500, {"error": "boom"})
        else:
            self._send(404, {"error": "nope"})

    def do_POST(self):
        SEEN.append(("POST", self.path, None))
        self._send(201, {"created": True})


@pytest.fixture(scope="module")
def base_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), App)
    t = threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.01), daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def run(tmp_path, base_url, script, path="/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", **env):
    full_env = {"PATH": path, "QA_BASE_URL": base_url, "QA_OUT": str(tmp_path / "out"), **env}
    res = subprocess.run(["bash", "-c", f'source "{HELPERS}"\n{script}'], capture_output=True, text=True, env=full_env, timeout=60)
    results = [json.loads(line) for line in (tmp_path / "out" / "results.jsonl").read_text().splitlines()]
    return res, results


def test_pass_and_fail_checks(tmp_path, base_url):
    res, rows = run(tmp_path, base_url, '''
        qa_case C1; api_call GET /items; check_http 200 "list ok"; check_json '.items | length' 2 "two items"; check_contains visible "body text"
        qa_case C2; api_call GET /boom; check_http 200 "should be ok"
        qa_summary''')
    assert [(r["case"], r["outcome"]) for r in rows] == [("C1", "pass"), ("C1", "pass"), ("C1", "pass"), ("C2", "fail")]
    assert "QA_RESULTS_JSON:{\"pass\":3,\"fail\":1,\"blocked\":0,\"not_run\":0}" in res.stdout
    assert "expected 200, got 500" in rows[3]["message"] and rows[3]["observed"].startswith("HTTP 500")
    assert Path(rows[3]["evidence"]).read_text().startswith("curl -X GET")


def test_json_check_mismatch_and_invalid_json(tmp_path, base_url):
    _, rows = run(tmp_path, base_url, '''
        qa_case C1; api_call GET /items; check_json '.items[0]' 9 "first item"
        HTTP_BODY="<html>"; check_json '.x' 1 "not json"''')
    assert [r["outcome"] for r in rows] == ["fail", "fail"] and "not valid JSON" in rows[1]["message"]


def test_mutating_calls_blocked_by_default_and_never_sent(tmp_path, base_url):
    SEEN.clear()
    _, rows = run(tmp_path, base_url, 'qa_case C1; api_call POST /items -d "{}"; check_http 201 "created"')
    assert [r["outcome"] for r in rows] == ["blocked"] and "blocked" in rows[0]["message"] and not rows[0]["infra"]
    assert ("POST", "/items", None) not in SEEN  # no request left the machine


def test_mutating_allowed_with_flag(tmp_path, base_url):
    _, rows = run(tmp_path, base_url, 'qa_case C1; api_call POST /items -d "{}"; check_http 201 "created"', QA_ALLOW_MUTATIONS="1")
    assert [r["outcome"] for r in rows] == ["pass"]


def test_cross_host_request_refused(tmp_path, base_url):
    SEEN.clear()
    _, rows = run(tmp_path, base_url, 'qa_case C1; api_call GET http://169.254.169.254/latest/meta-data; check_http 200 "x"')
    assert rows[0]["outcome"] == "blocked" and "outside the declared target" in rows[0]["message"] and SEEN == []


def test_connection_refused_is_infra_blocked_not_failure(tmp_path):
    _, rows = run(tmp_path, "http://127.0.0.1:1", 'qa_case C1; api_call GET /items; check_http 200 "up"; qa_summary')
    assert len(rows) == 1 and rows[0]["outcome"] == "blocked" and rows[0]["infra"] is True


def test_zero_executed_prints_warning(tmp_path):
    res, _ = run(tmp_path, "http://127.0.0.1:1", 'qa_case C1; api_call GET /items; qa_summary')
    assert "no check executed" in res.stdout and '"pass":0,"fail":0,"blocked":1' in res.stdout


def test_auth_header_sent_but_not_stored(tmp_path, base_url):
    SEEN.clear()
    _, rows = run(tmp_path, base_url, 'qa_case C1; api_call GET /items; check_http 200 "auth"', QA_AUTH_HEADER="Authorization: Bearer s3cr3t-token-value")
    assert SEEN[-1][2] == "Bearer s3cr3t-token-value"
    stored = (tmp_path / "out" / "results.jsonl").read_text() + Path(rows[0]["evidence"]).read_text()
    assert "s3cr3t-token-value" not in stored


def test_missing_base_url_is_blocked(tmp_path):
    res = subprocess.run(["bash", "-c", f'source "{HELPERS}"; qa_case C1; api_call GET /x; qa_summary'], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "QA_OUT": str(tmp_path / "o")}, timeout=30)
    assert "QA_BASE_URL is not set" in res.stdout


def test_records_feed_the_python_model(tmp_path, base_url):
    from scripts.qa import model
    run(tmp_path, base_url, 'qa_case C1; api_call GET /items; check_http 200 "ok"; qa_case C2; api_call GET /boom; check_http 200 "bad"')
    outs = model.case_outcomes(model.load_jsonl(tmp_path / "out" / "results.jsonl"))
    assert outs["C1"]["outcome"] == "pass" and outs["C2"]["outcome"] == "fail"


HOST_BYPASSES = [
    "http://evil.example#@{host}/x", "http://evil.example?@{host}/x", "https://{host}/x", "http://{host}.evil.example/x",
    "http://{host}@evil.example/x", "http://127.0.0.1:1/x", "http://{host}/x\nHost: evil",
]


def test_request_works_under_set_u_with_no_credentials(tmp_path, base_url):
    """macOS bash 3.2 treats an empty array as unbound under `set -u`; that must not turn a healthy target into an outage."""
    _, rows = run(tmp_path, base_url, "set -u\nqa_case C1; api_call GET /items; check_http 200 ok", path="/usr/bin:/bin")
    assert [r["outcome"] for r in rows] == ["pass"]


@pytest.mark.parametrize("template", HOST_BYPASSES)
def test_absolute_urls_outside_the_target_are_refused_and_never_sent(tmp_path, base_url, template):
    SEEN.clear()
    host = base_url.removeprefix("http://")
    url = template.format(host=host)
    res, rows = run(tmp_path, base_url, f'qa_case C1; api_call GET "$TARGET_URL"; check_http 200 x', TARGET_URL=url)
    assert [r["outcome"] for r in rows] == ["blocked"] and "outside the declared target" in rows[0]["message"], res.stdout
    assert SEEN == []


def test_same_target_with_query_and_fragment_is_allowed(tmp_path, base_url):
    _, rows = run(tmp_path, base_url, 'qa_case C1; api_call GET "$QA_BASE_URL/nope?x=1"; check_http 404 q; api_call GET "$QA_BASE_URL#frag"; check_http 404 f')
    assert [r["outcome"] for r in rows] == ["pass", "pass"]


@pytest.mark.parametrize("extra", [
    "-X POST", "-XPOST", "--request POST", "--request=POST", "-L", "--location", "-sL", "-sXPOST", "-so /tmp/leak", "-sx http://evil:1",
    "--proxy http://evil:1", "-x http://evil:1", "-K /etc/passwd", "--config x", "--next", "--url http://evil/", "-o /tmp/leak", "-D /tmp/hdr",
    "--resolve a:1:127.0.0.1", "-T /etc/passwd", "--netrc", "-v", "--insecure",
])
def test_curl_options_that_defeat_the_guards_are_refused(tmp_path, base_url, extra):
    SEEN.clear()
    _, rows = run(tmp_path, base_url, f"qa_case C1; api_call GET /items {extra}; check_http 200 x")
    assert [r["outcome"] for r in rows] == ["blocked"] and "not allowed" in rows[0]["message"] and SEEN == []


@pytest.mark.parametrize("extra", ["http://127.0.0.1:9002/second", "extra-word", "-H"])
def test_bare_extra_arguments_and_dangling_options_are_refused(tmp_path, base_url, extra):
    SEEN.clear()
    _, rows = run(tmp_path, base_url, f"qa_case C1; api_call GET /items {extra}; check_http 200 x")
    assert [r["outcome"] for r in rows] == ["blocked"] and SEEN == []


@pytest.mark.parametrize("extra", ["-d @/etc/passwd", "--data-binary @/etc/hosts", "-F file=@/etc/passwd", "--json @/etc/passwd"])
def test_local_files_cannot_be_read_into_a_request(tmp_path, base_url, extra):
    SEEN.clear()
    _, rows = run(tmp_path, base_url, f"qa_case C1; api_call POST /items {extra}; check_http 201 x", QA_ALLOW_MUTATIONS="1")
    assert [r["outcome"] for r in rows] == ["blocked"] and "local file" in rows[0]["message"] and SEEN == []


def test_the_documented_option_set_works(tmp_path, base_url):
    _, rows = run(tmp_path, base_url, """qa_case C1
api_call POST /items -H 'Content-Type: application/json' -H 'X-Trace: 1' -d '{"a":1}' -A qa-agent -b 'k=v' -m 5 --compressed -i
check_http 201 created
api_call GET /items -G --data-urlencode 'q=a b' -I
""", QA_ALLOW_MUTATIONS="1")
    assert rows[0]["outcome"] == "pass"


@pytest.mark.parametrize("method", ["FOO", "PURGE", "TRACE", "post", "delete"])
def test_only_get_head_options_are_safe_methods(tmp_path, base_url, method):
    SEEN.clear()
    _, rows = run(tmp_path, base_url, f"qa_case C1; api_call {method} /items; check_http 200 x")
    assert [r["outcome"] for r in rows] == ["blocked"] and "mutating call" in rows[0]["message"] and SEEN == []


def test_head_and_options_are_not_mutating(tmp_path, base_url):
    _, rows = run(tmp_path, base_url, "qa_case C1; api_call HEAD /items --max-time 5; qa_case C2; api_call OPTIONS /items; ")
    assert all(r["outcome"] != "blocked" or "mutating" not in r["message"] for r in rows)


def test_truncated_response_is_infrastructure_not_a_pass(tmp_path, base_url):
    _, rows = run(tmp_path, base_url, "qa_case C1; api_call GET /truncated; check_http 200 ok; check_contains tr bodyish")
    assert len(rows) == 1 and rows[0]["outcome"] == "blocked" and rows[0]["infra"] is True and "curl failed" in rows[0]["message"]


def test_401_without_credentials_is_blocked_with_credentials_it_is_a_failure(tmp_path, base_url):
    _, rows = run(tmp_path, base_url, "qa_case C1; api_call GET /secure; check_http 200 data")
    assert rows[0]["outcome"] == "blocked" and rows[0]["infra"] is True and "no credentials" in rows[0]["message"]
    _, rows = run(tmp_path / "w", base_url, "qa_case C1; api_call GET /secure; check_http 200 data", QA_AUTH_HEADER="Authorization: Bearer wrong")
    assert rows[0]["outcome"] == "pass"  # the fixture accepts any Authorization header
    _, rows = run(tmp_path / "x", base_url, "qa_case C1; api_call GET /secure; check_http 401 rejected")
    assert rows[0]["outcome"] == "pass"  # an expected 401 is a legitimate assertion, with or without credentials


def test_case_ids_cannot_escape_the_evidence_directory(tmp_path, base_url):
    _, rows = run(tmp_path, base_url, 'qa_case "../../etc/x y"; api_call GET /items; check_http 200 ok')
    assert rows[0]["case"] == ".._.._etc_x_y"
    from pathlib import Path
    assert Path(rows[0]["evidence"]).resolve().parent == (tmp_path / "out" / "evidence").resolve()


def test_huge_values_never_lose_the_record(tmp_path, base_url):
    _, rows = run(tmp_path, base_url, 'qa_case C1; big=$(head -c 300000 /dev/zero | tr "\\0" a); check_eq expected "$big" huge')
    assert len(rows) == 1 and rows[0]["outcome"] == "fail" and len(rows[0]["message"]) <= 500


def test_missing_jq_is_blocked_infrastructure_with_a_valid_record(tmp_path, base_url):
    bindir = tmp_path / "nojq"
    bindir.mkdir()
    import shutil
    for tool in ("bash", "mkdir", "tr", "head", "cat", "mktemp", "rm", "curl", "dirname"):
        located = shutil.which(tool)
        if located:
            (bindir / tool).symlink_to(located)
    res, rows = run(tmp_path, base_url, "qa_case C1; api_call GET /items; check_http 200 ok; qa_summary", path=str(bindir))
    assert len(rows) == 1 and rows[0]["outcome"] == "blocked" and rows[0]["infra"] is True
    assert "no check executed" in res.stdout


def test_a_rate_limited_request_is_retried_after_retry_after_and_then_judged_normally(tmp_path, base_url):
    LIMITED.clear()
    res, results = run(tmp_path, base_url, 'qa_case C1; api_call GET /limited-once; check_http 200 "works after the limit lifts"; qa_summary')
    assert [r["outcome"] for r in results] == ["pass"] and len(LIMITED) == 2


def test_a_target_that_keeps_rate_limiting_is_infrastructure_not_a_product_failure(tmp_path, base_url):
    res, results = run(tmp_path, base_url, 'qa_case C1; api_call GET /limited-always; check_http 200 "never gets through"; qa_summary', QA_RETRY_429="2")
    assert [(r["outcome"], r["infra"]) for r in results] == [("blocked", True)] and "rate limit" in results[0]["message"]


def test_a_429_that_the_case_expects_is_a_normal_check(tmp_path, base_url):
    res, results = run(tmp_path, base_url, 'qa_case C1; api_call GET /limited-always; check_http 429 "limit is enforced"; qa_summary', QA_RETRY_429="0")
    assert [r["outcome"] for r in results] == ["pass"]
