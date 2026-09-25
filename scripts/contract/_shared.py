"""Shared utilities for contract testing scripts."""
from __future__ import annotations

import glob
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def wait_for_service(url: str, label: str, retries: int = 15, delay: float = 2.0) -> bool:
    """Poll url until any status < 500 is returned. Returns True if ready."""
    for _ in range(retries):
        try:
            urllib.request.urlopen(url, timeout=2)
            print(f"[contract] {label} ready at {url}", flush=True)
            return True
        except urllib.error.HTTPError as e:
            if e.code < 500:
                print(f"[contract] {label} ready at {url} (HTTP {e.code})", flush=True)
                return True
            time.sleep(delay)
        except Exception:
            time.sleep(delay)
    return False


def probe_through_proxy(service_url: str, proxy_url: str) -> None:
    """Drive the service through the recording proxy to capture HTTP interactions.

    Uses an explicit ProxyHandler so no_proxy/NO_PROXY env vars are ignored.
    """
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    for path in ["/", "/health", "/api", "/api/Challenges", "/api/challenges",
                 "/docs", "/swagger", "/openapi.json", "/v1"]:
        try:
            req = urllib.request.Request(f"{service_url}{path}")
            try:
                resp = opener.open(req, timeout=5)
                print(f"[contract] probe {path} → {resp.status}", flush=True)
            except urllib.error.HTTPError as e:
                print(f"[contract] probe {path} → {e.code}", flush=True)
        except Exception:
            pass


def discover_endpoints(recordings_dir: Path) -> list[tuple[str, str]]:
    """Parse api-mock-service recording tree to extract (method, path) pairs.

    Directory structure: recordings/api_contracts/{path...}/{METHOD}/*.yaml
    """
    base = str(recordings_dir / "api_contracts")
    seen: set[tuple[str, str]] = set()
    endpoints: list[tuple[str, str]] = []
    for yaml_file in glob.glob(f"{base}/**/*.yaml", recursive=True):
        rel = os.path.dirname(yaml_file)[len(base):]
        parts = [p for p in rel.strip("/").split("/") if p]
        if parts and parts[-1] == parts[-1].upper() and parts[-1].isalpha():
            key = (parts[-1], "/" + "/".join(parts[:-1]))
            if key not in seen:
                seen.add(key)
                endpoints.append(key)
    return endpoints


def run_security_probes(
    service_url: str,
    endpoints: list[tuple[str, str]],
) -> list[dict]:
    """Inject SQLi, XSS, path-traversal, and long-input payloads into discovered endpoints.

    Returns a list of findings (5xx responses or SQL keyword leaks).
    """
    import urllib.parse

    sqli = "' OR '1'='1"
    xss = "<script>alert(1)</script>"

    # Cap at 20 endpoints to bound wall-clock time in the fuzz task.
    # Each GET endpoint gets 2 probes; each write endpoint gets 3.
    probes: list[tuple[str, str, str, dict | None]] = []
    for method, path in endpoints[:20]:
        if method == "GET":
            probes.append((f"sqli:{path}", "GET",
                            f"{service_url}{path}?q={urllib.parse.quote(sqli, safe='')}", None))
            probes.append((f"path_trav:{path}", "GET",
                            f"{service_url}{path}/../../etc/passwd", None))
        elif method in ("POST", "PUT", "PATCH"):
            probes.append((f"sqli:{path}", method, f"{service_url}{path}", {"input": sqli}))
            probes.append((f"xss:{path}", method, f"{service_url}{path}", {"input": xss}))
            probes.append((f"long:{path}", method, f"{service_url}{path}", {"input": "A" * 5000}))

    findings: list[dict] = []
    for name, method, url, body in probes:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            status, resp_body = resp.status, resp.read().decode()[:500]
        except urllib.error.HTTPError as e:
            status, resp_body = e.code, e.read().decode()[:500]
        except Exception as ex:
            status, resp_body = -1, str(ex)

        sqli_leak = any(kw in resp_body.lower() for kw in
                        ["sql", "syntax error", "pg_", "mysql", "sqlite", "hibernateexception"])
        stack_leak = any(kw in resp_body for kw in
                         ["java.lang.", "Caused by:", "at org.springframework"])
        is_finding = status >= 500 or sqli_leak or stack_leak
        if is_finding:
            severity = "critical" if (sqli_leak or stack_leak) else "medium"
            findings.append({"probe": name, "status": status, "severity": severity,
                              "sqli_leak": sqli_leak, "stack_leak": stack_leak})
        print(f"[contract] probe {name} → {status} finding={is_finding}", flush=True)

    return findings
