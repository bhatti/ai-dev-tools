"""Drive the service under test through the api-mock-service recording proxy.

Usage:
    python -m scripts.contract.record

Env:
    SERVICE_PORT       port the service listens on           (default 8080)
    MOCK_SERVICE_PORT  api-mock-service REST API port        (default 8081)
    PROXY_PORT         api-mock-service recording proxy port (default 8082)
    SERVICE_URL        full URL override (default http://localhost:SERVICE_PORT)
    WORKSPACE_DIR      workspace root                        (default /workspace)

Writes: WORKSPACE_DIR/record_result.json
Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.common.config import get_workspace_dir, load_config
from scripts.contract._shared import probe_through_proxy, wait_for_service


def main() -> None:
    config = load_config(required=[])
    ws = get_workspace_dir(config)
    service_port = os.environ.get("SERVICE_PORT", "8080")
    mock_port = os.environ.get("MOCK_SERVICE_PORT", "8081")
    proxy_port = os.environ.get("PROXY_PORT", "8082")
    service_url = os.environ.get("SERVICE_URL", "").strip() or f"http://localhost:{service_port}"

    (ws / "recordings").mkdir(parents=True, exist_ok=True)

    # Wait for AMS first (fast — Go binary starts in seconds).
    ams_ready = wait_for_service(
        f"http://localhost:{mock_port}/_health", "api-mock-service", retries=30
    )
    if not ams_ready:
        print(
            f"[record] WARNING: api-mock-service not reachable at :{mock_port} — recording skipped",
            flush=True,
        )
        _write_result(ws, test_exit=0, proxy_used=False, service_url=service_url, mock_port=mock_port)
        return

    # Wait for the service under test before probing — JVM-based services (WrongSecrets,
    # Spring Boot) take 30-90s to start and peak at 2G+ during startup.  Probing before
    # the JVM is ready adds HTTP pressure on top of the startup burst → OOM.
    # 45 retries × 2s = 90s max.
    svc_ready = wait_for_service(service_url, "service-under-test", retries=45, delay=2.0)
    if not svc_ready:
        print(
            f"[record] WARNING: service not reachable at {service_url} — recording skipped",
            flush=True,
        )
        _write_result(ws, test_exit=0, proxy_used=False, service_url=service_url, mock_port=mock_port)
        return

    proxy_url = f"http://localhost:{proxy_port}"
    test_exit = 0

    # Best-effort: run the repo test suite through the proxy (generates recordings).
    repo_dir = ws / "repo"
    if repo_dir.is_dir():
        env = {**os.environ, "HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url, "NO_PROXY": ""}
        try:
            result = subprocess.run(
                ["make", "test"], cwd=str(repo_dir), env=env,
                capture_output=True, text=True, timeout=1800,
            )
            test_exit = result.returncode
            if result.stdout:
                print(result.stdout[-2000:], flush=True)
            if result.stderr:
                print(result.stderr[-1000:], file=sys.stderr)
        except subprocess.TimeoutExpired:
            test_exit = 1
            print("[record] make test timed out after 1800s", file=sys.stderr)
        print(f"[record] make test exit={test_exit}", flush=True)

    # Also probe common REST paths directly — ensures recordings even without a test suite.
    probe_through_proxy(service_url, proxy_url)

    # Log how many scenario files AMS wrote so the artifact contains a diagnostic.
    n_scenarios = len(glob.glob(str(ws / "recordings" / "**" / "*.yaml"), recursive=True))
    print(f"[record] recordings: {n_scenarios} scenario files in {ws / 'recordings'}", flush=True)

    _write_result(ws, test_exit=test_exit, proxy_used=True, service_url=service_url, mock_port=mock_port)


def _write_result(ws: Path, *, test_exit: int, proxy_used: bool,
                  service_url: str, mock_port: str) -> None:
    data = {
        "test_exit_code": test_exit,
        "recordings_dir": str(ws / "recordings"),
        "proxy_used": proxy_used,
        "service_url": service_url,
        "mock_service_url": f"http://localhost:{mock_port}",
    }
    (ws / "record_result.json").write_text(json.dumps(data, indent=2))
    print(f"[record] complete: proxy_used={proxy_used} test_exit={test_exit}", flush=True)


if __name__ == "__main__":
    main()
