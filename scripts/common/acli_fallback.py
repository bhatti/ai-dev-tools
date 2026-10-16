"""Optional `acli` (Atlassian CLI) fallback for PR and issue context.

REST with the platform's tokens is the primary path (``pr_context``). When those tokens are absent but the machine
already has a working ``acli`` profile (a developer laptop, or a pod where ``atlassian_cli.write_cli_configs`` wrote
one), the same data is read through ``acli`` instead of failing. Every function returns ``None`` / an error string
rather than raising, so a missing or misconfigured acli simply means "no fallback".
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile

_TIMEOUT = 60


def _run(profile: str, args: list[str], max_bytes: int = 1 << 20) -> tuple[int, bytes, str]:
    """Run ``acli -p <profile> <args>``; output goes to a file so a huge diff cannot exhaust memory."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(["acli", "-p", profile, *args], stdout=out, stderr=err)
            rc = proc.wait(timeout=_TIMEOUT)
        except OSError as e:
            return 127, b"", type(e).__name__
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return 124, b"", f"acli timed out after {_TIMEOUT}s"
        out.seek(0)
        data = out.read(max_bytes)
        err.seek(0)
        return rc, data, err.read(300).decode("utf-8", "replace").strip()


def acli_ready(profile: str, product: str) -> bool:
    """True when acli is installed and ``profile`` authenticates (``bitbucket whoami`` / ``jira user me``)."""
    if not shutil.which("acli"):
        return False
    probe = ["bitbucket", "whoami"] if product == "bitbucket" else ["jira", "user", "me"]
    return _run(profile, ["-o", "json", *probe])[0] == 0


def _json(profile: str, args: list[str]) -> dict | None:
    rc, data, _ = _run(profile, ["-o", "json", *args])
    if rc != 0:
        return None
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def bitbucket_pr(workspace: str, repo: str, number: str, profile: str = "bitbucket") -> dict | None:
    """The PR in the shape ``fetch_prs_by_numbers`` returns, or None."""
    pr = _json(profile, ["bitbucket", "pr", "get", workspace, repo, str(number)])
    if not pr:
        return None
    return {"number": pr.get("id"), "title": pr.get("title", ""), "body": pr.get("description", "") or "",
            "state": pr.get("state", ""), "author": (pr.get("author") or {}).get("display_name", ""),
            "headRefName": ((pr.get("source") or {}).get("branch") or {}).get("name", ""),
            "baseRefName": ((pr.get("destination") or {}).get("branch") or {}).get("name", ""),
            "url": (((pr.get("links") or {}).get("html")) or {}).get("href", "")}


def bitbucket_diff(workspace: str, repo: str, number: str, limit: int, profile: str = "bitbucket") -> tuple[str, str]:
    """(diff_text, error); at most ``limit`` bytes are read."""
    rc, data, err = _run(profile, ["bitbucket", "pr", "diff", workspace, repo, str(number)], max_bytes=limit)
    if rc != 0:
        return "", f"acli pr diff exit {rc}: {err[:200]}"
    return data.decode("utf-8", "replace"), ""


def jira_issue(key: str, profile: str = "jira") -> dict | None:
    """The issue in the shape ``fetch_issue_details`` returns (body may be an ADF document), or None."""
    issue = _json(profile, ["jira", "issue", "get", "--json", key])
    fields = (issue or {}).get("fields")
    if not isinstance(fields, dict):
        return None
    return {"title": fields.get("summary", ""), "body": fields.get("description") or "",
            "labels": fields.get("labels") or [], "acceptance_criteria": "", "has_acceptance_criteria": None,
            "design_doc_links": []}
