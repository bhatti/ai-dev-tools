"""Contract validation and security fuzzing via api-mock-service.

Usage:
    python -m scripts.contract.fuzz

Env:
    SERVICE_URL        full URL of service under test (default http://localhost:SERVICE_PORT)
    SERVICE_PORT       service port                   (default 8080)
    MOCK_SERVICE_PORT  api-mock-service REST API port (default 8081)
    WORKSPACE_DIR      workspace root                 (default /workspace)

Reads:   WORKSPACE_DIR/recordings/api_contracts/**/*.yaml  (from record step)
Writes:  WORKSPACE_DIR/fuzz_result.json
Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from scripts.common.config import get_workspace_dir, load_config
from scripts.contract._shared import (
    discover_endpoints,
    run_security_probes,
    wait_for_service,
)


def _run_contract_replay(ams_base: str, service_url: str) -> dict:
    body = json.dumps({"base_url": service_url, "execution_times": 3}).encode()
    req = urllib.request.Request(
        f"{ams_base}/_contracts/default",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        print(
            f"[fuzz] contracts: succeeded={data.get('succeeded', 0)} "
            f"failed={data.get('failed', 0)}",
            flush=True,
        )
        return data
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()[:300]
        print(f"[fuzz] contract replay HTTP {e.code}: {body_txt}", flush=True)
        return {"succeeded": 0, "failed": 0, "error": str(e)}
    except Exception as e:
        print(f"[fuzz] contract replay error: {e}", flush=True)
        return {"succeeded": 0, "failed": 0, "error": str(e)}


def _upload_openapi_spec(ams_base: str, ws: Path) -> None:
    spec = ws / "repo" / "openapi" / "openapi.yaml"
    if not spec.exists():
        return
    req = urllib.request.Request(
        f"{ams_base}/_oapi",
        data=spec.read_bytes(),
        headers={"Content-Type": "application/yaml"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[fuzz] uploaded OpenAPI spec from {spec}", flush=True)
    except Exception as e:
        print(f"[fuzz] OpenAPI upload failed (non-fatal): {e}", flush=True)


def _write_junit(ws: Path, findings: list[dict], iterations: int) -> None:
    suite = Element("testsuite", name="contract-fuzz-tests")
    suite.set("tests", str(iterations))
    suite.set("failures", str(len(findings)))
    for i, f in enumerate(findings):
        tc = SubElement(suite, "testcase",
                        name=f"fuzz-{i}-{f.get('probe', 'unknown')}",
                        classname=f.get('probe', 'unknown'))
        failure = SubElement(tc, "failure",
                             message=f.get("probe", ""),
                             type=f.get("severity", "medium"))
        failure.text = json.dumps(f, indent=2)
    xml = b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(suite)
    (ws / "fuzz_results.xml").write_bytes(xml)


def main() -> None:
    config = load_config(required=[])
    ws = get_workspace_dir(config)
    service_port = os.environ.get("SERVICE_PORT", "8080")
    mock_port = os.environ.get("MOCK_SERVICE_PORT", "8081")
    service_url = os.environ.get("SERVICE_URL", "").strip() or f"http://localhost:{service_port}"
    ams_base = f"http://localhost:{mock_port}"

    # 30 retries × 2s = 60s max — enough for JVM-based services to start.
    ams_ready = wait_for_service(f"{ams_base}/_health", "api-mock-service", retries=30)

    # Contract replay requires AMS; security probes use recordings and run regardless.
    contract_resp: dict = {"succeeded": 0, "failed": 0}
    if ams_ready:
        _upload_openapi_spec(ams_base, ws)
        contract_resp = _run_contract_replay(ams_base, service_url)
    else:
        print("[fuzz] api-mock-service not reachable — skipping contract replay", flush=True)

    # Discover endpoints from record-task artifact recordings; probes run against the
    # live service regardless of AMS so findings are always collected.
    endpoints = discover_endpoints(ws / "recordings")
    print(f"[fuzz] discovered {len(endpoints)} endpoints: {endpoints}", flush=True)

    findings = run_security_probes(service_url, endpoints) if endpoints else []
    critical = sum(1 for f in findings if f.get("severity") == "critical")
    print(f"[fuzz] {len(endpoints)} endpoints, {len(findings)} findings, {critical} critical",
          flush=True)

    # Iteration count: GET→2 probes, write→3 probes (matches run_security_probes).
    capped = endpoints[:20]
    get_count = sum(1 for m, _ in capped if m == "GET")
    write_count = len(capped) - get_count
    iterations = get_count * 2 + write_count * 3

    fuzz: dict = {
        "iterations": iterations,
        "findings": findings,
        "contract_results": contract_resp,
        "ams_used": ams_ready,
    }
    (ws / "fuzz_result.json").write_text(json.dumps(fuzz, indent=2))
    _write_junit(ws, findings, iterations)

    # Summary consumed by mq.report for Slack notification.
    status = "FAIL" if critical else "PASS"
    methods = {m for m, _ in capped}
    probe_type_names = ["SQLi", "path-traversal"] if capped else []
    if methods - {"GET"}:
        probe_type_names += ["XSS", "oversized-payload"]
    if any(f.get("cred_leak") for f in findings):
        probe_type_names.append("credential-exposure")
    summary = {
        "pr_number": os.environ.get("PR_NUMBER", ""),
        "contract_breaking_changes": contract_resp.get("failed", 0),
        "fuzz_iterations": iterations,
        "fuzz_findings": len(findings),
        "critical_findings": critical,
        "endpoints_scanned": len(capped),
        "probe_types": probe_type_names,
        "status": status,
    }
    (ws / "contract_test_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"[fuzz] complete: iterations={iterations} findings={len(findings)} "
          f"critical={critical} status={status} ams_used={ams_ready}", flush=True)


if __name__ == "__main__":
    main()
