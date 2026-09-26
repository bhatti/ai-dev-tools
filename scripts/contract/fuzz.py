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
import stat
import subprocess
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


def _debug_filesystem(ws: Path) -> None:
    """Dump filesystem diagnostics to stdout to help trace artifact copy issues."""
    print("[fuzz-debug] ===== FILESYSTEM DIAGNOSTICS =====", flush=True)

    # 1. Show working dir and /workspace top-level
    try:
        cwd = os.getcwd()
        print(f"[fuzz-debug] CWD={cwd}", flush=True)
    except Exception as e:
        print(f"[fuzz-debug] CWD error: {e}", flush=True)

    for top in [ws, Path("/workspace"), Path("/")]:
        if top.exists():
            try:
                children = sorted(top.iterdir())
                st = top.stat()
                mode = stat.filemode(st.st_mode)
                print(
                    f"[fuzz-debug] ls {top}/ (uid={st.st_uid} mode={mode}): "
                    f"{[c.name for c in children[:20]]}",
                    flush=True,
                )
            except Exception as e:
                print(f"[fuzz-debug] ls {top} error: {e}", flush=True)
        else:
            print(f"[fuzz-debug] {top} does NOT exist", flush=True)

    # 2. Find ALL yaml files anywhere under /workspace and /recordings (wrong-path canary)
    for search_root in ["/workspace", "/recordings", "/tmp"]:
        try:
            r = subprocess.run(
                ["find", search_root, "-name", "*.yaml", "-type", "f"],
                capture_output=True, text=True, timeout=10,
            )
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            print(
                f"[fuzz-debug] yaml files under {search_root}: {len(lines)} files "
                f"first_3={lines[:3]}",
                flush=True,
            )
        except Exception as e:
            print(f"[fuzz-debug] find {search_root} error: {e}", flush=True)

    # 3. Permission / ownership of critical directories
    for check in [
        ws / "recordings",
        ws / "recordings" / "api_contracts",
        Path("/workspace/recordings"),
        Path("/workspace/recordings/api_contracts"),
        Path("/recordings"),
        Path("/recordings/api_contracts"),
    ]:
        if check.exists():
            try:
                st = check.stat()
                mode = stat.filemode(st.st_mode)
                children = list(check.iterdir())
                print(
                    f"[fuzz-debug] {check}: uid={st.st_uid} gid={st.st_gid} mode={mode} "
                    f"children={[c.name for c in children[:10]]}",
                    flush=True,
                )
            except Exception as e:
                print(f"[fuzz-debug] stat {check} error: {e}", flush=True)
        else:
            print(f"[fuzz-debug] {check}: does NOT exist", flush=True)

    # 4. Check the formicary extracted-artifacts dir (under /tmp)
    try:
        r = subprocess.run(
            ["find", "/tmp", "-path", "*/extracted-artifacts*", "-maxdepth", "8"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        print(f"[fuzz-debug] extracted-artifacts paths: {lines[:10]}", flush=True)
    except Exception as e:
        print(f"[fuzz-debug] find extracted-artifacts error: {e}", flush=True)

    print("[fuzz-debug] ===== END DIAGNOSTICS =====", flush=True)


def main() -> None:
    config = load_config(required=[])
    ws = get_workspace_dir(config)
    _debug_filesystem(ws)
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
    recordings_dir = ws / "recordings"
    contracts_dir = recordings_dir / "api_contracts"
    yaml_files = list(contracts_dir.rglob("*.yaml")) if contracts_dir.exists() else []
    print(
        f"[fuzz] recordings_dir={recordings_dir} exists={recordings_dir.exists()} "
        f"contracts_dir_exists={contracts_dir.exists()} yaml_count={len(yaml_files)} "
        f"first_3={[str(f) for f in yaml_files[:3]]}",
        flush=True,
    )
    endpoints = discover_endpoints(recordings_dir)
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
