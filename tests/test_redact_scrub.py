"""Tests for scripts/common/redact.py, env_scrub.py and the escaping/redaction helpers in report_bundle.py."""

from __future__ import annotations

import contextlib
import json
import signal

import pytest

from scripts.common import env_scrub
from scripts.common.redact import redact, redact_obj
from scripts.common.report_bundle import md_cell, write_bundle
from scripts.common.report_renderer import render_simple_html


@contextlib.contextmanager
def completes_within(seconds: int):
    """Hang guard for linear-time claims: a catastrophic regex fails the test instead of stalling the suite."""
    def boom(*_):
        raise TimeoutError(f"did not finish within {seconds}s")
    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


class TestRedactShapes:
    @pytest.mark.parametrize("raw,secret", [
        ('{"password": "hunter2hunter2"}', "hunter2hunter2"),
        ('{"token":"abc123def456"}', "abc123def456"),
        ('{"client_secret":"zzzzzzzz1"}', "zzzzzzzz1"),
        ('{"access_token":"opaque123456"}', "opaque123456"),
        ('"api_key":"k1234567"', "k1234567"),
        ('private_key = "abcdefgh1234"', "abcdefgh1234"),
        ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI"),
        ("password: 'hunter2hunter2'", "hunter2hunter2"),
        ("export GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwx", "ghp_abcdefghijklmnopqrstuvwx"),
        ("Authorization: Bearer abcdefghijklmnop123", "abcdefghijklmnop123"),
        ("Cookie: session=abcdef123456", "abcdef123456"),
        ("postgres://user:s3cr3tpw@db:5432/x", "s3cr3tpw"),
        ("key AKIAABCDEFGHIJKLMNOP here", "AKIAABCDEFGHIJKLMNOP"),
        ("xoxb-1234567890-abcdefghij", "xoxb-1234567890-abcdefghij"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.abcdefghijkl", "eyJhbGciOiJIUzI1NiJ9"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEA"),
        ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq (log cut off, no END marker)", "MIIEvQIBADANBgkq"),
        ('{\\"password\\":\\"hunter2hunter2\\"}', "hunter2hunter2"),                       # stringified JSON inside a log line
        ('secret_key: "a\\"b hunter2hunter2"', "hunter2hunter2"),                               # escaped quote inside the value
        ("mytool --password hunter2hunter2 --user bob", "hunter2hunter2"),
        ("mytool --token=abc123def456 run", "abc123def456"),
    ])
    def test_secret_removed(self, raw, secret):
        assert secret not in redact(raw)

    def test_plain_text_untouched(self):
        for text in ("AssertionError: expected 1 got 2", "the token bucket refilled", "", "password reset page loaded"):
            assert redact(text) == text

    def test_key_names_survive_so_the_context_stays_readable(self):
        assert redact('{"password": "x1234567"}').startswith('{"password":')


class TestRedactIsLinear:
    @pytest.mark.parametrize("payload", ["a-" * 40_000, "{{" * 20_000, "password" * 8_000, "token-" * 20_000,
                                          "-----BEGIN PRIVATE KEY-----" * 3_000, "x=" * 50_000, "a" * 1_000_000])
    def test_hostile_input_finishes(self, payload):
        with completes_within(10):
            redact(payload)


class TestRedactObj:
    def test_nested_structures_and_secret_keys(self):
        out = redact_obj({"meta": {"password": "plain", "note": "token=abc12345678"}, "list": ["ok", "Bearer abcdefghijklmnop123"], "n": 3, "none": None})
        assert out["meta"]["password"] == "<redacted>" and "abc12345678" not in out["meta"]["note"]
        assert "abcdefghijklmnop123" not in out["list"][1] and out["n"] == 3 and out["none"] is None and out["list"][0] == "ok"


class TestMdCell:
    def test_escapes_what_could_become_markup(self):
        out = md_cell("a | b [x](javascript:alert(1)) <img src=x> `c`\nnext")
        assert "\n" not in out and "\\|" in out and "\\[" in out and "\\<" in out and "\\`" in out

    def test_redacts(self):
        assert "hunter2hunter2" not in md_cell("password=hunter2hunter2")


class TestRenderedHtmlIsInert:
    HOSTILE = [
        "[click](javascript:alert(1))", "[x](data:text/html;base64,AAAA)", '[x](http://q" onmouseover="alert(1))',
        '![a](x" onerror="alert(2))', "<script>alert(3)</script>", "<img src=x onerror=alert(4)>",
    ]

    @pytest.mark.parametrize("payload", HOSTILE)
    def test_raw_markdown_cannot_produce_active_html(self, payload):
        html = render_simple_html("t", f"para {payload}\n\n| a | b |\n| --- | --- |\n| {payload} | y |\n")
        lowered = html.lower()
        assert 'href="javascript' not in lowered and 'href="data:' not in lowered and "<script>" not in lowered
        assert "<img src=x" not in lowered and 'onerror="' not in lowered and 'onmouseover="' not in lowered

    @pytest.mark.parametrize("payload", HOSTILE)
    def test_escaped_cells_render_as_literal_text(self, payload):
        html = render_simple_html("t", f"| a | b |\n| --- | --- |\n| {md_cell(payload)} | y |\n")
        assert "<a href" not in html and "<img" not in html and html.count("<td>") == 2

    def test_escaped_pipe_stays_inside_one_cell(self):
        html = render_simple_html("t", "| a | b |\n| --- | --- |\n| x \\| y | z |\n")
        assert html.count("<td>") == 2 and "x | y" in html

    def test_legitimate_links_and_relative_evidence_links_still_work(self):
        html = render_simple_html("t", "[docs](https://example.com/a?b=1) [shot](evidence/c2.png) ![i](evidence/x.png)")
        assert '<a href="https://example.com/a?b=1">docs</a>' in html and '<a href="evidence/c2.png">shot</a>' in html
        assert '<img src="evidence/x.png"' in html


class TestWriteBundle:
    def test_all_three_files_are_redacted(self, tmp_path):
        write_bundle(tmp_path, "T", "# r\npassword=hunter2hunter2\n", {"findings": [{"snippet": 'k = "AKIAABCDEFGHIJKLMNOP"'}], "token": "tok_abcdef123456"})
        blob = "".join((tmp_path / n).read_text() for n in ("report.md", "report.html", "result.json"))
        assert "hunter2hunter2" not in blob and "AKIAABCDEFGHIJKLMNOP" not in blob and "tok_abcdef123456" not in blob
        assert json.loads((tmp_path / "result.json").read_text())["token"] == "<redacted>"


class TestScrubbedEnvironment:
    def test_platform_credentials_are_dropped_and_ordinary_variables_kept(self, monkeypatch):
        for name in ("GH_TOKEN", "BITBUCKET_TOKEN", "JIRA_API_TOKEN", "ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_PRIVATE_KEY"):
            monkeypatch.setenv(name, "secret")
        monkeypatch.setenv("PATH_LIKE_SETTING", "keep")
        monkeypatch.setenv("QA_PASSWORD", "app-under-test")
        env = env_scrub.scrubbed_environ({"EXTRA": "1"})
        assert not set(env_scrub.PLATFORM_SECRETS) & set(env) and env["PATH_LIKE_SETTING"] == "keep" and env["EXTRA"] == "1"
        assert "QA_PASSWORD" not in env  # code under test never needs the credentials for the target
        assert env_scrub.scrubbed_environ(keep_qa=True)["QA_PASSWORD"] == "app-under-test"


class TestCredentialLookingNames:
    @pytest.mark.parametrize("name", ["ATLASSIAN_API_TOKEN", "JENKINS_TOKEN", "QUEEN_SSH_KEY", "CLAUDE_CODE_MESSAGING_TOKEN", "NPM_TOKEN", "MY_API_KEY",
                                      "SLACK_WEBHOOK_URL", "DB_PASSWORD", "DB_PASSWD", "CLIENT_SECRET", "SSH_AUTH_SOCK", "PRIVATE_KEY", "HTTP_AUTH", "AUTHORIZATION"])
    def test_credential_looking_variables_are_dropped(self, monkeypatch, name):
        monkeypatch.setenv(name, "s3cr3t")
        assert name not in env_scrub.scrubbed_environ()

    @pytest.mark.parametrize("name", ["PATH", "HOME", "AUTHOR_NAME", "LANG", "TZ", "CI", "NODE_ENV", "PYTHONPATH", "OAUTHLIB_NOTE"])
    def test_ordinary_variables_survive(self, monkeypatch, name):
        monkeypatch.setenv(name, "v")
        assert env_scrub.scrubbed_environ()[name] == "v"

    def test_explicitly_kept_variables_pass_through(self, monkeypatch):
        monkeypatch.setenv("NPM_TOKEN", "registry-token")
        assert env_scrub.scrubbed_environ(keep=("NPM_TOKEN",))["NPM_TOKEN"] == "registry-token"

    def test_cli_keep_flag(self, monkeypatch):
        import subprocess
        import sys
        env = {**__import__("os").environ, "NPM_TOKEN": "registry-token", "GH_TOKEN": "platform-secret"}
        res = subprocess.run([sys.executable, "-m", "scripts.common.env_scrub", "--keep", "NPM_TOKEN", "--", sys.executable, "-c",
                              "import os; print(os.environ.get('NPM_TOKEN'), os.environ.get('GH_TOKEN'))"], env=env, capture_output=True, text=True)
        assert res.stdout.strip() == "registry-token None"


class TestScrubCli:
    def test_runs_the_command_without_platform_credentials_and_returns_its_status(self, monkeypatch):
        import subprocess
        import sys
        env = {**__import__("os").environ, "GH_TOKEN": "platform-secret", "KEEP_ME": "yes"}
        res = subprocess.run([sys.executable, "-m", "scripts.common.env_scrub", "--", sys.executable, "-c",
                              "import os, sys; print(os.environ.get('GH_TOKEN'), os.environ.get('KEEP_ME')); sys.exit(7)"],
                             env=env, capture_output=True, text=True)
        assert res.returncode == 7 and res.stdout.strip() == "None yes"

    def test_missing_command_and_missing_binary(self):
        assert env_scrub.main([]) == 2
        assert env_scrub.main(["--", "no-such-binary-xyz"]) == 127

    def test_keep_qa_flag_retains_target_credentials(self, monkeypatch):
        monkeypatch.setenv("QA_PASSWORD", "target")
        assert env_scrub.scrubbed_environ(keep_qa=True)["QA_PASSWORD"] == "target"
