"""Tests for scripts/qa/ui_runner.py logic using a fake page (the real browser is covered by the pod tests)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.qa import ui_runner as ui
from scripts.qa.model import append_jsonl

BASE = "http://127.0.0.1:8080"


class TimeoutErr(Exception):
    pass


class FakeLocator:
    def __init__(self, page, key):
        self.page, self.key = page, key
        self.first = self

    def wait_for(self, state="visible", timeout=0):
        present = self.key in self.page.visible
        if (state == "visible" and not present) or (state == "hidden" and present):
            raise TimeoutErr(f"Timeout {timeout}ms exceeded waiting for {self.key} to be {state}\nCall log: ...")

    def count(self):
        return self.page.counts.get(self.key, 0)


class FakePage:
    def __init__(self, visible=(), counts=None, title="Shop", fail_on=(), statuses=None):
        self.visible, self.counts, self._title, self.fail_on = set(visible), counts or {}, title, set(fail_on)
        self.statuses = statuses or {}
        self.calls: list[tuple] = []
        self.url = BASE + "/"

    def _do(self, name, *a, **k):
        self.calls.append((name, *a))
        if name in self.fail_on:
            raise TimeoutErr(f"Timeout waiting for selector {a[0]}")

    def goto(self, url, **k):
        self._do("goto", url)
        self.url = url
        return type("Resp", (), {"status": self.statuses.get(url.removeprefix(BASE), 200)})()

    def wait_for_load_state(self, state="load", timeout=0):
        self.calls.append(("settle", state))

    def click(self, sel, **k):
        self._do("click", sel)

    def fill(self, sel, val, **k):
        self._do("fill", sel, val)

    def press(self, sel, key, **k):
        self._do("press", sel, key)

    def select_option(self, sel, val, **k):
        self._do("select", sel, val)

    def check(self, sel, **k):
        self._do("check", sel)

    def locator(self, sel):
        return FakeLocator(self, sel)

    def get_by_text(self, text):
        return FakeLocator(self, f"text={text}")

    def wait_for_url(self, pattern, **k):
        if pattern.strip("*/") not in self.url:
            raise TimeoutErr(f"Timeout waiting for url {pattern}")

    def title(self):
        return self._title

    def screenshot(self, path, **k):
        open(path, "wb").write(b"png")


def run(page, steps, tmp_path, console=(), bad=(), **case_kw):
    return ui.run_case(page, {"id": "C1", "steps": steps, **case_kw}, BASE, tmp_path, list(console), list(bad), timeout=200)


def outcomes(recs):
    return [r["outcome"] for r in recs]


class TestUrls:
    def test_relative_joined(self):
        assert ui.resolve_url(BASE, "/cart?x=1") == BASE + "/cart?x=1"
        assert ui.resolve_url(BASE + "/app/", "cart") == BASE + "/app/cart"

    def test_same_origin_absolute_allowed(self):
        assert ui.resolve_url(BASE, BASE + "/x") == BASE + "/x"

    @pytest.mark.parametrize("target", ["http://evil.example/x", "https://127.0.0.1:8080/x", "//evil.example/x", "http://127.0.0.1:9999/x"])
    def test_cross_origin_refused(self, target):
        with pytest.raises(ui.StepError, match="cross-origin"):
            ui.resolve_url(BASE, target)


class TestValidateSpec:
    def test_spec_cannot_choose_the_target(self):
        assert any("QA_BASE_URL" in p for p in ui.validate_spec({"base_url": "http://evil.example", "cases": [{"id": "C1", "steps": [{"goto": "/"}]}]}))

    def test_login_requires_a_success_selector(self):
        spec = {"login": {"username_selector": "#u", "password_selector": "#p", "submit_selector": "b"}, "smoke": ["/"]}
        assert any("success_selector" in p for p in ui.validate_spec(spec))

    def test_smoke_ids_are_reserved(self):
        assert any("reserved" in p for p in ui.validate_spec({"cases": [{"id": "SMOKE1", "steps": [{"goto": "/"}]}]}))

    def test_valid(self):
        assert ui.validate_spec({"cases": [{"id": "C1", "steps": [{"goto": "/"}, {"expect_text": "hi"}]}]}) == []

    def test_problems(self):
        probs = " | ".join(ui.validate_spec({"cases": [{"id": "A", "steps": [{"goto": "/", "click": "x"}, {"bogus": 1}]}, {"id": "A", "steps": []}]}))
        assert "exactly one of" in probs and "duplicate case id A" in probs
        assert ui.validate_spec({}) == ["spec has neither cases nor smoke pages"]

    def test_smoke_expands_to_cases(self):
        cases = ui.smoke_cases(["/", "/a"])
        assert [c["id"] for c in cases] == ["SMOKE1", "SMOKE2"] and cases[1]["steps"][0] == {"goto": "/a"}


class TestRunCase:
    def test_successful_flow_has_per_expectation_records_and_final_pass(self, tmp_path):
        page = FakePage(visible={"text=limit of 10", ".error"})
        recs = run(page, [{"goto": "/cart"}, {"fill": {"selector": "#qty", "value": "11"}}, {"click": "text=Add"},
                          {"expect_text": "limit of 10"}, {"expect_visible": ".error"}, {"screenshot": "after"}], tmp_path)
        assert outcomes(recs) == ["pass", "pass", "pass"]  # two expectations + the console/network check; a screenshot is not a check
        assert ("goto", BASE + "/cart") in page.calls and ("fill", "#qty", "11") in page.calls
        assert open(recs[-1]["screenshot"], "rb").read() == b"png"  # evidence attached to the deciding record

    def test_missing_expected_text_fails_with_screenshot_and_stops(self, tmp_path):
        recs = run(FakePage(), [{"goto": "/"}, {"expect_text": "never shown"}, {"click": "never reached"}], tmp_path)
        assert outcomes(recs) == ["fail"] and "step failed: TimeoutErr" in recs[0]["message"] and recs[0]["screenshot"]
        assert "Call log" not in recs[0]["message"]  # only the first line is kept

    def test_action_failure_is_fail_not_crash(self, tmp_path):
        recs = run(FakePage(fail_on={"click"}), [{"click": "#missing"}], tmp_path)
        assert outcomes(recs) == ["fail"] and recs[0]["command"].startswith('{"click"')

    def test_cross_origin_goto_is_blocked_not_failed(self, tmp_path):
        page = FakePage()
        recs = run(page, [{"goto": "http://169.254.169.254/latest"}], tmp_path)
        assert outcomes(recs) == ["blocked"] and not page.calls

    def test_console_errors_fail_the_case(self, tmp_path):
        recs = run(FakePage(visible={".ok"}), [{"expect_visible": ".ok"}], tmp_path, console=["Uncaught TypeError: x is undefined"])
        assert outcomes(recs) == ["pass", "fail"] and "console error" in recs[1]["message"] and "TypeError" in recs[1]["observed"]

    def test_console_errors_can_be_opted_out(self, tmp_path):
        recs = run(FakePage(visible={".ok"}), [{"expect_visible": ".ok"}], tmp_path, console=["boom"], fail_on_console_error=False)
        assert "fail" not in outcomes(recs)

    def test_noise_ignored_via_env(self, tmp_path, monkeypatch):
        console = ["Failed to load analytics.js", "GET /favicon.ico 404"]
        recs = run(FakePage(visible={".ok"}), [{"expect_visible": ".ok"}], tmp_path, console=console)
        assert outcomes(recs) == ["pass", "fail"] and "analytics.js" in recs[1]["observed"]  # the favicon noise is built in, analytics is not
        monkeypatch.setenv("QA_CONSOLE_IGNORE", "analytics.js")
        recs = run(FakePage(visible={".ok"}), [{"expect_visible": ".ok"}], tmp_path, console=console)
        assert outcomes(recs) == ["pass", "pass"] and recs[-1]["message"] == "no console errors or 5xx responses"

    def test_5xx_fails_the_case(self, tmp_path):
        recs = run(FakePage(visible={".ok"}), [{"expect_visible": ".ok"}], tmp_path, bad=["503 http://x/api"])
        assert outcomes(recs) == ["pass", "fail"] and "5xx" in recs[1]["message"]

    def test_actions_only_case_proves_nothing(self, tmp_path):
        recs = run(FakePage(), [{"goto": "/"}, {"click": "#a"}], tmp_path)
        assert outcomes(recs) == ["inconclusive"]

    def test_screenshot_is_not_a_check(self, tmp_path):
        recs = run(FakePage(), [{"goto": "/"}, {"screenshot": "page"}], tmp_path)
        assert outcomes(recs) == ["inconclusive"] and recs[0]["screenshot"]

    def test_generated_smoke_case_passes_when_the_page_loads_cleanly(self, tmp_path):
        recs = ui.run_case(FakePage(), ui.smoke_cases(["/"])[0], BASE, tmp_path, [], [], timeout=200)
        assert outcomes(recs) == ["pass"] and "page loaded" in recs[0]["message"] and recs[0]["screenshot"]

    def test_smoke_on_a_missing_page_fails(self, tmp_path):
        recs = ui.run_case(FakePage(statuses={"/gone": 404}), ui.smoke_cases(["/gone"])[0], BASE, tmp_path, [], [], timeout=200)
        assert outcomes(recs) == ["fail"] and "HTTP 404" in recs[0]["message"] and recs[0]["screenshot"]

    def test_smoke_with_console_errors_fails(self, tmp_path):
        recs = ui.run_case(FakePage(), ui.smoke_cases(["/"])[0], BASE, tmp_path, ["Uncaught ReferenceError"], [], timeout=200)
        assert outcomes(recs) == ["fail"]

    def test_goto_fails_on_server_errors_and_honours_an_expected_status(self, tmp_path):
        assert outcomes(run(FakePage(statuses={"/boom": 503}), [{"goto": "/boom"}, {"expect_visible": ".x"}], tmp_path)) == ["fail"]
        recs = run(FakePage(statuses={"/missing": 404}), [{"goto": "/missing", "expect_status": 404}], tmp_path)
        assert outcomes(recs) == ["pass", "pass"] and "HTTP 404" in recs[0]["message"]
        recs = run(FakePage(), [{"goto": "/", "expect_status": 404}], tmp_path)
        assert outcomes(recs) == ["fail"] and "expected 404" in recs[0]["message"]

    def test_case_ids_cannot_escape_the_screenshot_directory(self, tmp_path):
        recs = ui.run_case(FakePage(), {"id": "../../x y", "steps": [{"goto": "/"}, {"screenshot": "a/../b"}, {"expect_visible": ".z"}]}, BASE, tmp_path, [], [], timeout=200)
        shot = ui.Path(recs[-1]["screenshot"]).resolve()
        assert shot.parent == (tmp_path / "screenshots").resolve()

    def test_mutating_case_blocked_by_default(self, tmp_path):
        page = FakePage()
        recs = run(page, [{"click": "text=Delete account"}], tmp_path, mutating=True)
        assert outcomes(recs) == ["blocked"] and not page.calls
        recs = ui.run_case(page, {"id": "C2", "mutating": True, "steps": [{"click": "x"}]}, BASE, tmp_path, [], [], allow_mutations=True)
        assert ("click", "x") in page.calls and recs

    def test_expect_not_visible_title_count_url(self, tmp_path):
        page = FakePage(visible={"#a"}, counts={"li": 3}, title="Shop - Cart")
        page.url = BASE + "/cart"
        recs = run(page, [{"expect_not_visible": ".spinner"}, {"expect_title": "Cart$"}, {"expect_count": {"selector": "li", "count": 3}},
                          {"expect_url": "**/cart"}], tmp_path)
        assert outcomes(recs) == ["pass"] * 5
        assert ("settle", "networkidle") in page.calls  # absence checks wait for the page to settle first
        recs = run(page, [{"expect_count": {"selector": "li", "count": 5}}, {"expect_title": "Checkout"}], tmp_path)
        assert outcomes(recs) == ["fail"] and recs[0]["observed"] == "3"  # the first failed expectation ends the case
        assert outcomes(run(page, [{"expect_title": "Checkout"}], tmp_path)) == ["fail"]

    def test_records_feed_the_model(self, tmp_path):
        from scripts.qa import model
        recs = run(FakePage(visible={".ok"}), [{"expect_visible": ".ok"}], tmp_path)
        append_jsonl(tmp_path / "results.jsonl", recs)
        assert model.case_outcomes(model.load_jsonl(tmp_path / "results.jsonl"))["C1"]["outcome"] == "pass"


class TestSandbox:
    def test_container_markers_disable_sandbox(self, monkeypatch):
        monkeypatch.setattr(ui.os, "geteuid", lambda: 1000)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.setattr(ui.Path, "exists", lambda self: False)
        assert ui._needs_no_sandbox() is False
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        assert ui._needs_no_sandbox() is True
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST")
        monkeypatch.setattr(ui.os, "geteuid", lambda: 0)
        assert ui._needs_no_sandbox() is True


class TestLaunchChromium:
    class PW:
        def __init__(self, error=None):
            self.error, self.chromium, self.args = error, self, None

        def launch(self, headless=True, args=()):
            self.args = list(args)
            if self.error:
                raise RuntimeError(self.error)
            return "browser"

    def test_launches_chromium(self):
        pw = self.PW()
        assert ui.launch_chromium(pw) == "browser"

    def test_a_missing_browser_binary_says_how_to_install_it(self):
        with pytest.raises(RuntimeError, match="playwright install chromium"):
            ui.launch_chromium(self.PW("BrowserType.launch: Executable doesn't exist at /x/chrome"))

    def test_other_launch_errors_are_not_masked(self):
        with pytest.raises(RuntimeError, match="missing shared libraries"):
            ui.launch_chromium(self.PW("missing shared libraries"))


class TestMain:
    def test_invalid_spec_exit_2(self, tmp_path, capsys):
        spec = tmp_path / "s.json"
        spec.write_text("{}")
        assert ui.main(["--spec", str(spec), "--out-dir", str(tmp_path / "o")]) == 2
        assert "invalid spec" in capsys.readouterr().out

    def test_playwright_missing_is_blocked_with_infra_records(self, tmp_path, monkeypatch):
        spec = tmp_path / "s.json"
        spec.write_text(json.dumps({"smoke": ["/"], "cases": [{"id": "C1", "steps": [{"goto": "/"}]}]}))
        monkeypatch.setattr(ui, "run_spec", lambda *a, **k: (_ for _ in ()).throw(ImportError("no playwright")))
        assert ui.main(["--spec", str(spec), "--out-dir", str(tmp_path / "o")]) == ui.EXIT_BLOCKED
        rows = [json.loads(line) for line in (tmp_path / "o" / "results.jsonl").read_text().splitlines()]
        assert [(r["case"], r["outcome"], r["infra"]) for r in rows] == [("SMOKE1", "blocked", True), ("C1", "blocked", True)]

    def test_blocked_message_keeps_the_real_error(self, tmp_path, monkeypatch, capsys):
        spec = tmp_path / "s.json"
        spec.write_text(json.dumps({"cases": [{"id": "C1", "steps": [{"goto": "/"}]}]}))
        monkeypatch.setattr(ui, "run_spec", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Executable doesn't exist at /x")))
        ui.main(["--spec", str(spec), "--out-dir", str(tmp_path / "o")])
        assert "Executable doesn't exist at /x" in capsys.readouterr().err

    def test_browser_launch_failure_is_blocked(self, tmp_path, monkeypatch):
        spec = tmp_path / "s.json"
        spec.write_text(json.dumps({"cases": [{"id": "C1", "steps": [{"goto": "/"}]}]}))
        monkeypatch.setattr(ui, "run_spec", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Executable doesn't exist")))
        assert ui.main(["--spec", str(spec), "--out-dir", str(tmp_path / "o")]) == ui.EXIT_BLOCKED

    def test_success_prints_results_line(self, tmp_path, monkeypatch, capsys):
        spec = tmp_path / "s.json"
        spec.write_text(json.dumps({"cases": [{"id": "C1", "steps": [{"goto": "/"}]}]}))
        monkeypatch.setattr(ui, "run_spec", lambda *a, **k: [ui._record("C1", "pass", "ok"), ui._record("C1", "fail", "bad")])
        assert ui.main(["--spec", str(spec), "--out-dir", str(tmp_path / "o")]) == 0
        assert 'QA_RESULTS_JSON:{"pass": 1, "fail": 1, "blocked": 0}' in capsys.readouterr().out

    def test_login_without_credentials_is_a_step_error(self, monkeypatch):
        monkeypatch.delenv("QA_USERNAME", raising=False)
        monkeypatch.delenv("QA_PASSWORD", raising=False)

        class P:  # noqa: D401 - never reached: credentials are checked first
            pass
        with pytest.raises(ui.StepError, match="QA_USERNAME"):
            ui._login(P(), {"username_selector": "#u", "password_selector": "#p", "submit_selector": "b"}, BASE, 1000)


class FakeRequest:
    def __init__(self, url):
        self.url = url


class FakeRoute:
    def __init__(self):
        self.action = None

    def continue_(self):
        self.action = "continue"

    def abort(self):
        self.action = "abort"


class FakeContext:
    def __init__(self, page, state="STATE", fail_new_page=False):
        self.page, self.state, self.handler, self.closed, self.fail_new_page = page, state, None, False, fail_new_page

    def route(self, pattern, handler):
        self.handler = handler

    def new_page(self):
        if self.fail_new_page:
            raise RuntimeError("browser crashed")
        return self.page

    def storage_state(self):
        return self.state

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, page_factory, fail_new_page=False):
        self.page_factory, self.contexts, self.kwargs, self.fail_new_page = page_factory, [], [], fail_new_page

    def new_context(self, **kwargs):
        self.kwargs.append(kwargs)
        ctx = FakeContext(self.page_factory(), fail_new_page=self.fail_new_page)
        self.contexts.append(ctx)
        return ctx


class ListeningPage(FakePage):
    def on(self, event, cb):
        pass


SPEC_LOGIN = {"login": {"username_selector": "#u", "password_selector": "#p", "submit_selector": "b", "success_selector": "#in"},
              "cases": [{"id": "C1", "steps": [{"expect_visible": "#in"}]}, {"id": "C2", "steps": [{"expect_visible": "#in"}]}]}


class TestIsolatedCase:
    def run(self, browser, case, spec=None, login_state=None, allowed=None):
        return ui._run_case_isolated(browser, case, spec or {}, BASE, Path("."), allowed or {ui.origin(BASE)}, False, False, login_state)

    def test_context_blocks_service_workers_downloads_and_foreign_origins(self, tmp_path):
        browser = FakeBrowser(lambda: ListeningPage(visible={"#a"}))
        records, _ = ui._run_case_isolated(browser, {"id": "C1", "steps": [{"expect_visible": "#a"}]}, {}, BASE, tmp_path, {ui.origin(BASE)}, False, False, None)
        assert outcomes(records) == ["pass", "pass"]
        assert browser.kwargs[0]["service_workers"] == "block" and browser.kwargs[0]["accept_downloads"] is False
        handler = browser.contexts[0].handler
        for url, expected in [(BASE + "/api", "continue"), ("http://evil.example/steal", "abort"), ("http://127.0.0.1:9999/x", "abort"),
                              ("https://127.0.0.1:8080/x", "abort"), ("data:text/plain,hi", "continue"), ("about:blank", "continue")]:
            route = FakeRoute()
            handler(route, FakeRequest(url))
            assert route.action == expected, url

    def test_extra_origins_come_only_from_the_environment_allowlist(self, monkeypatch):
        monkeypatch.setenv("QA_ALLOW_ORIGINS", "https://cdn.example.com, http://127.0.0.1:9000")
        allowed = ui._allowed_origins(BASE)
        assert allowed == {ui.origin(BASE), ("https", "cdn.example.com"), ("http", "127.0.0.1:9000")}

    def test_login_happens_once_and_its_state_is_reused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QA_USERNAME", "u")
        monkeypatch.setenv("QA_PASSWORD", "p")
        pages = []

        def make():
            pages.append(ListeningPage(visible={"#in"}))
            return pages[-1]

        browser = FakeBrowser(make)
        r1, state = ui._run_case_isolated(browser, SPEC_LOGIN["cases"][0], SPEC_LOGIN, BASE, tmp_path, {ui.origin(BASE)}, False, False, None)
        r2, state2 = ui._run_case_isolated(browser, SPEC_LOGIN["cases"][1], SPEC_LOGIN, BASE, tmp_path, {ui.origin(BASE)}, False, False, state)
        assert state == "STATE" and state2 == "STATE"
        assert any(c[0] == "fill" for c in pages[0].calls) and not any(c[0] == "fill" for c in pages[1].calls)
        assert "storage_state" not in browser.kwargs[0] and browser.kwargs[1]["storage_state"] == "STATE"
        assert outcomes(r1) == outcomes(r2) == ["pass", "pass"]

    def test_failed_login_blocks_the_case_with_the_real_reason_and_never_runs_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QA_USERNAME", "u")
        monkeypatch.setenv("QA_PASSWORD", "wrong")
        browser = FakeBrowser(lambda: ListeningPage(visible=set()))  # the success selector never appears
        records, state = ui._run_case_isolated(browser, SPEC_LOGIN["cases"][0], SPEC_LOGIN, BASE, tmp_path, {ui.origin(BASE)}, False, False, None)
        assert outcomes(records) == ["blocked"] and records[0]["infra"] is True and "login failed: TimeoutErr" in records[0]["message"] and state is None

    def test_a_crash_in_one_case_is_recorded_for_that_case_only(self, tmp_path):
        browser = FakeBrowser(lambda: ListeningPage(), fail_new_page=True)
        records, _ = ui._run_case_isolated(browser, {"id": "C1", "steps": []}, {}, BASE, tmp_path, {ui.origin(BASE)}, False, False, None)
        assert outcomes(records) == ["blocked"] and records[0]["infra"] is True and "browser crashed" in records[0]["message"]
        assert browser.contexts[0].closed is True  # the context is closed even when the case blew up

    def test_run_spec_requires_the_target_from_the_environment(self, monkeypatch, tmp_path):
        monkeypatch.delenv("QA_BASE_URL", raising=False)
        with pytest.raises(ui.StepError, match="QA_BASE_URL"):
            ui.run_spec({"smoke": ["/"]}, tmp_path, tmp_path / "r.jsonl")


class TestLoginFailureIsNotRetried:
    def run_all(self, tmp_path, monkeypatch, visible):
        monkeypatch.setenv("QA_USERNAME", "u")
        monkeypatch.setenv("QA_PASSWORD", "wrong")
        pages = []

        def make():
            pages.append(ListeningPage(visible=visible))
            return pages[-1]

        browser = FakeBrowser(make)
        records = ui.run_cases(browser, SPEC_LOGIN["cases"], SPEC_LOGIN, BASE, tmp_path, tmp_path / "r.jsonl", {ui.origin(BASE)}, False, False)
        return records, pages, browser

    def test_a_bad_login_is_attempted_once_and_blocks_the_remaining_cases(self, tmp_path, monkeypatch):
        records, pages, browser = self.run_all(tmp_path, monkeypatch, visible=set())  # the success selector never shows up
        assert [(r["case"], r["outcome"], r["infra"]) for r in records] == [("C1", "blocked", True), ("C2", "blocked", True)]
        assert records[0]["message"].startswith("login failed") and "not run" in records[1]["message"] and "login failed" in records[1]["message"]
        assert len(browser.contexts) == 1 and sum(1 for c in pages[0].calls if c[0] == "fill") == 2  # one username + one password entry, ever

    def test_results_are_written_per_case_as_they_finish(self, tmp_path, monkeypatch):
        records, _, _ = self.run_all(tmp_path, monkeypatch, visible={"#in"})
        from scripts.qa.model import load_jsonl
        assert [r["case"] for r in load_jsonl(tmp_path / "r.jsonl")] == [r["case"] for r in records] and [r["outcome"] for r in records] == ["pass", "pass", "pass", "pass"]

    def test_missing_credentials_also_stop_after_the_first_case(self, tmp_path, monkeypatch):
        monkeypatch.delenv("QA_USERNAME", raising=False)
        monkeypatch.delenv("QA_PASSWORD", raising=False)
        browser = FakeBrowser(lambda: ListeningPage())
        records = ui.run_cases(browser, SPEC_LOGIN["cases"], SPEC_LOGIN, BASE, tmp_path, tmp_path / "r.jsonl", {ui.origin(BASE)}, False, False)
        assert [r["outcome"] for r in records] == ["blocked", "blocked"] and len(browser.contexts) == 1
