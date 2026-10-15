"""Run declarative UI test cases with Playwright (Python) and append results as JSON Lines.

Spec (JSON) -- selectors and steps only, never credentials or the target URL (that is QA_BASE_URL):
    {"login": {"path": "/login", "username_selector": "#user", "password_selector": "#pass",
               "submit_selector": "button[type=submit]", "success_selector": "#dashboard"},
     "smoke": ["/", "/settings"],
     "cases": [{"id": "C3", "mutating": false, "steps": [
         {"goto": "/cart"}, {"click": "text=Add item"}, {"fill": {"selector": "#qty", "value": "11"}},
         {"expect_text": "limit of 10"}, {"expect_visible": ".error"}, {"expect_url": "**/cart"}, {"screenshot": "cart-error"}]}]}

Safety (the page under test and the PR text are untrusted):
  * the target is QA_BASE_URL only; credentials only from QA_USERNAME / QA_PASSWORD; no arbitrary JavaScript step
  * every browser request to another origin is aborted (extra origins only via QA_ALLOW_ORIGINS, comma separated);
    service workers are blocked; each case gets a fresh isolated browser context
  * cases marked ``mutating`` are recorded as blocked unless --allow-mutations / QA_ALLOW_MUTATIONS=1
A case passes only on evidence: at least one expectation passed (smoke pages: the page loaded), and no console
error or HTTP 5xx happened. ``goto`` fails on HTTP >= 400 unless the step says ``"expect_status": 404``. Every wait
is a condition wait with a timeout. Console noise can be ignored with QA_CONSOLE_IGNORE (regexes, comma separated).
Results are appended per case, so a crash never discards finished cases.

Usage:
    python3 -m scripts.qa.ui_runner --spec ui-spec.json --out-dir qa-out/candidate [--side candidate] [--allow-mutations] [--headed]
Exit codes: 0 ran, 2 invalid spec/config, 3 BLOCKED (Playwright/browser unavailable; infra records written).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from scripts.common.exit_codes import EXIT_BLOCKED, EXIT_USAGE
from scripts.qa.model import append_jsonl

DEFAULT_TIMEOUT_MS = 10_000
_NOISE = [r"favicon\.ico", r"chrome-extension://", r"Failed to load resource: .* 404 .*favicon"]
_ACTIONS = {"goto", "click", "fill", "press", "select", "check", "wait_for", "screenshot"}
_EXPECTS = {"expect_text", "expect_not_text", "expect_visible", "expect_not_visible", "expect_url", "expect_title", "expect_count"}
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


class StepError(Exception):
    """A step was malformed or unsafe (reported as a blocked record, not a crash)."""


def origin(url: str) -> tuple[str, str]:
    p = urlparse(url)
    return p.scheme, p.netloc.lower()


def resolve_url(base: str, target: str) -> str:
    """Relative targets join onto base; absolute ones must be same-origin."""
    full = urljoin(base.rstrip("/") + "/", target)
    if origin(full) != origin(base):
        raise StepError(f"cross-origin navigation refused: {origin(full)[1]} is not {origin(base)[1]}")
    return full


def validate_spec(spec: dict) -> list[str]:
    problems = []
    if not spec.get("cases") and not spec.get("smoke"):
        problems.append("spec has neither cases nor smoke pages")
    if "base_url" in spec:
        problems.append("base_url is not allowed in the spec: the target is QA_BASE_URL")
    login = spec.get("login")
    if login and not login.get("success_selector"):
        problems.append("login needs a success_selector, otherwise cases would run unauthenticated after a failed login")
    ids = set()
    for case in spec.get("cases", []):
        cid = case.get("id")
        if not cid:
            problems.append("a case has no id")
        elif cid in ids:
            problems.append(f"duplicate case id {cid}")
        elif cid.startswith("SMOKE"):
            problems.append(f"case id {cid} is reserved for generated smoke checks")
        ids.add(cid)
        for i, step in enumerate(case.get("steps", [])):
            keys = set(step) & (_ACTIONS | _EXPECTS)
            if len(keys) != 1:
                problems.append(f"case {cid} step {i}: exactly one of {sorted(_ACTIONS | _EXPECTS)} is required, got {sorted(step)}")
    return problems


def smoke_cases(paths: list[str]) -> list[dict]:
    """One check per page: it loads (HTTP < 400) without console errors or 5xx responses."""
    return [{"id": f"SMOKE{i}", "title": f"smoke {p}", "smoke": True, "steps": [{"goto": p}, {"screenshot": "smoke"}]} for i, p in enumerate(paths, 1)]


def _needs_no_sandbox() -> bool:
    """Chromium's sandbox cannot start inside most containers/pods or as root; the container is the isolation boundary there."""
    return (os.geteuid() == 0 or bool(os.environ.get("KUBERNETES_SERVICE_HOST")) or Path("/.dockerenv").exists()
            or Path("/run/.containerenv").exists())


def _ignored(text: str) -> bool:
    extra = [p for p in os.environ.get("QA_CONSOLE_IGNORE", "").split(",") if p.strip()]
    return any(re.search(p, text) for p in _NOISE + extra)


def _record(case: str, outcome: str, message: str, command: str = "", observed: str = "", screenshot: str = "", infra: bool = False) -> dict:
    return {"case": case, "outcome": outcome, "message": message, "command": command, "observed": observed[:500],
            "screenshot": screenshot, "infra": infra}


def _shot(page: Any, out_dir: Path, case: str, name: str) -> str:
    path = out_dir / "screenshots" / f"{_SAFE_ID.sub('_', case)}-{_SAFE_ID.sub('_', name)}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:  # noqa: BLE001 - a failed screenshot must not hide the real result
        return ""
    return str(path)


def _eventually(check: Callable[[], bool], timeout_ms: int) -> bool:
    """Condition wait for values Playwright cannot wait on itself (counts, titles): poll until true or the deadline."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        threading.Event().wait(0.1)


def _settle(page: Any, timeout_ms: int) -> None:
    """Absence checks pass vacuously right after an action unless the page has settled first (best effort)."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:  # noqa: BLE001 - long polling pages never go idle; the absence check below still has its own wait
        pass


def execute_step(page: Any, step: dict, base: str, case_id: str, out_dir: Path, timeout: int = DEFAULT_TIMEOUT_MS) -> dict | str | None:
    """Run one step. Actions return None, a screenshot returns its path, expectations return a result record."""
    if "goto" in step:
        url = resolve_url(base, step["goto"])
        response = page.goto(url, timeout=timeout, wait_until="load")
        status = getattr(response, "status", None)
        wanted = step.get("expect_status")
        if wanted is not None and status != wanted:
            return _record(case_id, "fail", f"GET {step['goto']} returned HTTP {status}, expected {wanted}", f"goto {step['goto']}", observed=f"HTTP {status}")
        if wanted is None and status is not None and status >= 400:
            return _record(case_id, "fail", f"GET {step['goto']} returned HTTP {status}", f"goto {step['goto']}", observed=f"HTTP {status}")
        return _record(case_id, "pass", f"{step['goto']} loaded (HTTP {status})", f"goto {step['goto']}", observed=f"HTTP {status}") if wanted is not None else None
    if "click" in step:
        page.click(step["click"], timeout=timeout)
    elif "fill" in step:
        page.fill(step["fill"]["selector"], step["fill"]["value"], timeout=timeout)
    elif "press" in step:
        page.press(step["press"]["selector"], step["press"]["key"], timeout=timeout)
    elif "select" in step:
        page.select_option(step["select"]["selector"], step["select"]["value"], timeout=timeout)
    elif "check" in step:
        page.check(step["check"], timeout=timeout)
    elif "wait_for" in step:
        page.locator(step["wait_for"]).first.wait_for(state="visible", timeout=timeout)
    elif "screenshot" in step:
        return _shot(page, out_dir, case_id, step["screenshot"])
    elif "expect_text" in step:
        page.get_by_text(step["expect_text"]).first.wait_for(state="visible", timeout=timeout)
        return _record(case_id, "pass", f"text visible: {step['expect_text']}", f"expect_text {step['expect_text']!r}")
    elif "expect_not_text" in step:
        _settle(page, timeout)
        page.get_by_text(step["expect_not_text"]).first.wait_for(state="hidden", timeout=timeout)
        return _record(case_id, "pass", f"text absent: {step['expect_not_text']}", f"expect_not_text {step['expect_not_text']!r}")
    elif "expect_visible" in step:
        page.locator(step["expect_visible"]).first.wait_for(state="visible", timeout=timeout)
        return _record(case_id, "pass", f"visible: {step['expect_visible']}", f"expect_visible {step['expect_visible']}")
    elif "expect_not_visible" in step:
        _settle(page, timeout)
        page.locator(step["expect_not_visible"]).first.wait_for(state="hidden", timeout=timeout)
        return _record(case_id, "pass", f"not visible: {step['expect_not_visible']}", f"expect_not_visible {step['expect_not_visible']}")
    elif "expect_url" in step:
        page.wait_for_url(step["expect_url"], timeout=timeout)
        return _record(case_id, "pass", f"url matches {step['expect_url']}", f"expect_url {step['expect_url']}", observed=page.url)
    elif "expect_title" in step:
        ok = _eventually(lambda: re.search(step["expect_title"], page.title()) is not None, timeout)
        title = page.title()
        return _record(case_id, "pass" if ok else "fail", f"title {'matches' if ok else 'does not match'} {step['expect_title']}", "expect_title", observed=title)
    elif "expect_count" in step:
        spec = step["expect_count"]
        ok = _eventually(lambda: page.locator(spec["selector"]).count() == int(spec["count"]), timeout)
        n = page.locator(spec["selector"]).count()
        return _record(case_id, "pass" if ok else "fail", f"{spec['selector']} count {n} (expected {spec['count']})", "expect_count", observed=str(n))
    else:
        raise StepError(f"unknown step {sorted(step)}")
    return None


def run_case(page: Any, case: dict, base: str, out_dir: Path, console: list[str], bad_responses: list[str],
             allow_mutations: bool = False, timeout: int = DEFAULT_TIMEOUT_MS) -> list[dict]:
    """Run one case on a prepared page; ``console``/``bad_responses`` are filled by the page listeners."""
    cid = case["id"]
    if case.get("mutating") and not allow_mutations:
        return [_record(cid, "blocked", "mutating UI case blocked (pass --allow-mutations)")]
    records: list[dict] = []
    shot = ""
    navigated = False
    for step in case.get("steps", []):
        try:
            result = execute_step(page, step, base, cid, out_dir, timeout)
        except StepError as e:
            records.append(_record(cid, "blocked", str(e), json.dumps(step)[:200]))
            return records
        except Exception as e:  # noqa: BLE001 - selector/timeouts/navigation errors become evidence
            msg = f"{type(e).__name__}: {str(e).strip().splitlines()[0] if str(e).strip() else ''}"
            records.append(_record(cid, "fail", f"step failed: {msg}", json.dumps(step)[:200], screenshot=_shot(page, out_dir, cid, "failure")))
            return records
        if "goto" in step:
            navigated = True
        if isinstance(result, str):
            shot = result or shot
        elif result:
            records.append(result)
            if result["outcome"] == "fail":
                _attach(result, _shot(page, out_dir, cid, "failure") or shot)
                return records
    errors = [m for m in console if not _ignored(m)]
    if errors and case.get("fail_on_console_error", True):
        records.append(_record(cid, "fail", f"{len(errors)} console error(s)", "console", observed=errors[0]))
    if bad_responses:
        records.append(_record(cid, "fail", f"{len(bad_responses)} HTTP 5xx response(s)", "network", observed=bad_responses[0]))
    checks = [r for r in records if r["outcome"] in ("pass", "fail")]
    if not checks and case.get("smoke") and navigated:
        records.append(_record(cid, "pass", "page loaded without console errors or 5xx responses", "goto"))
    elif not checks:
        records.append(_record(cid, "inconclusive", "the case made no check (add an expect_* step)"))
    elif all(r["outcome"] == "pass" for r in records):
        records.append(_record(cid, "pass", "no console errors or 5xx responses", "console+network"))
    if shot and records:  # the screenshot is evidence for the case as a whole: attach it to its deciding record
        _attach(records[-1], shot)
    return records


def _attach(record: dict, shot: str) -> dict:
    record["screenshot"] = record.get("screenshot") or shot
    return record


def _login(page: Any, login: dict, base: str, timeout: int) -> None:
    user, pw = os.environ.get("QA_USERNAME"), os.environ.get("QA_PASSWORD")
    if not (user and pw):
        raise StepError("login configured but QA_USERNAME / QA_PASSWORD are not set")
    page.goto(resolve_url(base, login.get("path", "/login")), timeout=timeout)
    page.fill(login["username_selector"], user, timeout=timeout)
    page.fill(login["password_selector"], pw, timeout=timeout)
    page.click(login["submit_selector"], timeout=timeout)
    page.locator(login["success_selector"]).first.wait_for(state="visible", timeout=timeout)


def launch_chromium(pw: Any, headless: bool = True) -> Any:
    """Launch Chromium. The ai-dev-tools image ships it in PLAYWRIGHT_BROWSERS_PATH; elsewhere run `playwright install chromium`."""
    try:
        return pw.chromium.launch(headless=headless, args=["--no-sandbox"] if _needs_no_sandbox() else [])
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e):
            raise RuntimeError("Chromium is not installed for this user (image without PLAYWRIGHT_BROWSERS_PATH, or a local run): "
                               "run `python3 -m playwright install chromium`") from e
        raise


def _allowed_origins(base: str) -> set[tuple[str, str]]:
    extra = [o.strip() for o in os.environ.get("QA_ALLOW_ORIGINS", "").split(",") if o.strip()]
    return {origin(base), *(origin(o) for o in extra)}


def run_spec(spec: dict, out_dir: Path, results_path: Path, allow_mutations: bool = False, headless: bool = True) -> list[dict]:
    """Execute every case in a fresh browser context, appending each case's records to ``results_path`` as it finishes.

    Requires Playwright (ImportError otherwise). A crash in one case is recorded for that case only.
    """
    base = os.environ.get("QA_BASE_URL", "")
    if not base:
        raise StepError("QA_BASE_URL is not set (run inside scripts.qa.env_manager or export it)")
    from playwright.sync_api import sync_playwright  # lazy: unit tests and non-UI runs do not need it

    cases = [*smoke_cases(spec.get("smoke", [])), *spec.get("cases", [])]
    with sync_playwright() as pw:
        browser = launch_chromium(pw, headless)
        try:
            return run_cases(browser, cases, spec, base, out_dir, results_path, _allowed_origins(base),
                             os.environ.get("QA_INSECURE_TLS") == "1", allow_mutations)
        finally:
            browser.close()


def run_cases(browser: Any, cases: list[dict], spec: dict, base: str, out_dir: Path, results_path: Path, allowed: set,
              insecure: bool, allow_mutations: bool) -> list[dict]:
    """Run the cases one by one, appending each case's records as soon as it finishes.

    A login that fails (or cannot be attempted) blocks every remaining case without trying again: repeating a bad
    login for each case would hammer the account until it locks.
    """
    all_records: list[dict] = []
    login_state: dict | None = None
    login_error = ""
    for case in cases:
        if login_error:
            records = [_record(case["id"], "blocked", f"not run: {login_error}", infra=True)]
        else:
            records, login_state = _run_case_isolated(browser, case, spec, base, out_dir, allowed, insecure, allow_mutations, login_state)
            if spec.get("login") and login_state is None and records and records[0]["outcome"] == "blocked" and records[0]["message"].startswith(("login ",)):
                login_error = records[0]["message"]
        append_jsonl(results_path, records)
        all_records.extend(records)
    return all_records


def _run_case_isolated(browser: Any, case: dict, spec: dict, base: str, out_dir: Path, allowed: set, insecure: bool,
                       allow_mutations: bool, login_state: dict | None) -> tuple[list[dict], dict | None]:
    """One case in its own context. Login happens once; its storage state is reused so accounts are not hammered."""
    console: list[str] = []
    bad: list[str] = []
    kwargs: dict = {"viewport": spec.get("viewport") or {"width": 1280, "height": 800}, "ignore_https_errors": insecure,
                    "accept_downloads": False, "service_workers": "block"}
    if login_state:
        kwargs["storage_state"] = login_state
    ctx = browser.new_context(**kwargs)
    try:
        ctx.route("**/*", lambda route, req: route.continue_() if (origin(req.url) in allowed or req.url.startswith(("about:", "data:", "blob:"))) else route.abort())
        page = ctx.new_page()
        page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console.append(str(e)))
        page.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 500 else None)
        if spec.get("login") and login_state is None:
            try:
                _login(page, spec["login"], base, DEFAULT_TIMEOUT_MS)
                login_state = ctx.storage_state()
            except StepError as e:
                return [_record(case["id"], "blocked", str(e))], None
            except Exception as e:  # noqa: BLE001
                return [_record(case["id"], "blocked", f"login failed: {type(e).__name__}: {str(e).strip().splitlines()[0] if str(e).strip() else ''}", infra=True)], None
            console.clear()
            bad.clear()
        return run_case(page, case, base, out_dir, console, bad, allow_mutations), login_state
    except Exception as e:  # noqa: BLE001 - one broken case must not discard the others
        return [_record(case["id"], "blocked", f"case could not run: {type(e).__name__}: {str(e)[:200]}", infra=True)], login_state
    finally:
        ctx.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--spec", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--allow-mutations", action="store_true", default=os.environ.get("QA_ALLOW_MUTATIONS") == "1")
    p.add_argument("--headed", action="store_true")
    a = p.parse_args(argv)
    try:
        spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"cannot read spec: {e}"}))
        return EXIT_USAGE
    problems = validate_spec(spec)
    if problems:
        print(json.dumps({"error": "invalid spec", "problems": problems}))
        return EXIT_USAGE
    out = Path(a.out_dir)
    results = out / "results.jsonl"
    ids = [c["id"] for c in [*smoke_cases(spec.get("smoke", [])), *spec.get("cases", [])]]
    try:
        records = run_spec(spec, out, results, a.allow_mutations, headless=not a.headed)
    except ImportError:
        append_jsonl(results, [_record(i, "blocked", "Playwright is not installed (pip install playwright && playwright install chromium)", infra=True) for i in ids])
        print("[qa-ui] BLOCKED: Playwright unavailable", file=sys.stderr)
        return EXIT_BLOCKED
    except StepError as e:
        print(json.dumps({"error": str(e)}))
        return EXIT_USAGE
    except Exception as e:  # noqa: BLE001 - browser launch failures are infrastructure
        append_jsonl(results, [_record(i, "blocked", f"browser could not run: {type(e).__name__}: {str(e)[:200]}", infra=True) for i in ids])
        print(f"[qa-ui] BLOCKED: {type(e).__name__}: {str(e)[:600]}", file=sys.stderr)
        return EXIT_BLOCKED
    count = lambda o: sum(1 for r in records if r["outcome"] == o)
    print(f"QA_RESULTS_JSON:{json.dumps({'pass': count('pass'), 'fail': count('fail'), 'blocked': count('blocked')})}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
