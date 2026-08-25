"""Connectivity check for all AI workflow integrations.

Tests: GitHub, Jira, Bitbucket, Slack, Claude (Bedrock or direct API).
Every check is optional and non-fatal — the script always exits 0.
Results are written to /workspace/connectivity_result.json.

Usage:
    python -m scripts.check_connectivity

Optional env (checks skipped when absent):
    GH_TOKEN, GH_ORG, GH_REPO
    JIRA_API_TOKEN, JIRA_EMAIL, JIRA_BASE_URL
    BITBUCKET_TOKEN, BITBUCKET_USERNAME, BITBUCKET_WORKSPACE, BITBUCKET_REPO
    SLACK_BOT_TOKEN, SLACK_CHANNEL (or SLACK_CHANNEL)
    CLAUDE_CODE_USE_BEDROCK / ANTHROPIC_API_KEY, ANTHROPIC_BEDROCK_BASE_URL
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
TIMEOUT = 10


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

_results: list[dict] = []


def _record(name: str, ok: bool, detail: str) -> None:
    status = "OK" if ok else "FAIL"
    icon = "✅" if ok else "❌"
    print(f"{icon} [{name}] {status}: {detail}", flush=True)
    _results.append({"check": name, "status": status, "detail": detail})


def _skip(name: str, reason: str) -> None:
    print(f"⏭️  [{name}] SKIP: {reason}", flush=True)
    _results.append({"check": name, "status": "SKIP", "detail": reason})


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def check_github() -> None:
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        _skip("github", "GH_TOKEN not set")
        return
    try:
        resp = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            login = resp.json().get("login", "?")
            _record("github", True, f"authenticated as @{login}")
        else:
            _record("github", False, f"HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        _record("github", False, str(e))

    # Repo access (optional)
    org = os.environ.get("GH_ORG", "")
    repo = os.environ.get("GH_REPO", "")
    if not (org and repo):
        _skip("github-repo", "GH_ORG / GH_REPO not set")
        return
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{org}/{repo}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            _record("github-repo", True, f"can read {org}/{repo}")
        else:
            _record("github-repo", False, f"HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        _record("github-repo", False, str(e))


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

def check_jira() -> None:
    token = os.environ.get("JIRA_API_TOKEN", "")
    email = os.environ.get("JIRA_EMAIL", "")
    base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    if not (token and email and base_url):
        _skip("jira", "JIRA_API_TOKEN / JIRA_EMAIL / JIRA_BASE_URL not set")
        return
    try:
        resp = requests.get(
            f"{base_url}/rest/api/3/myself",
            auth=(email, token),
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            name = data.get("displayName") or data.get("emailAddress", "?")
            _record("jira", True, f"authenticated as {name} at {base_url}")
        else:
            _record("jira", False, f"HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        _record("jira", False, str(e))


# ---------------------------------------------------------------------------
# Bitbucket
# ---------------------------------------------------------------------------

def check_bitbucket() -> None:
    token = os.environ.get("BITBUCKET_TOKEN", "")
    username = os.environ.get("BITBUCKET_USERNAME", "")
    workspace = os.environ.get("BITBUCKET_WORKSPACE", "")
    if not (token and username):
        _skip("bitbucket", "BITBUCKET_TOKEN / BITBUCKET_USERNAME not set")
        return
    try:
        resp = requests.get(
            "https://api.bitbucket.org/2.0/user",
            auth=(username, token),
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            nick = data.get("display_name") or data.get("nickname", "?")
            _record("bitbucket", True, f"authenticated as {nick}")
        else:
            _record("bitbucket", False, f"HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        _record("bitbucket", False, str(e))

    if not workspace:
        _skip("bitbucket-workspace", "BITBUCKET_WORKSPACE not set")
        return
    try:
        resp = requests.get(
            f"https://api.bitbucket.org/2.0/workspaces/{workspace}",
            auth=(username, token),
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            _record("bitbucket-workspace", True, f"can read workspace {workspace}")
        else:
            _record("bitbucket-workspace", False, f"HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        _record("bitbucket-workspace", False, str(e))


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def check_slack() -> None:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        _skip("slack", "SLACK_BOT_TOKEN not set")
        return
    try:
        resp = requests.get(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        data = resp.json() if resp.ok else {}
        if data.get("ok"):
            team = data.get("team", "?")
            user = data.get("user", "?")
            _record("slack", True, f"authenticated as {user} in team {team}")
        else:
            error = data.get("error", resp.text[:120])
            _record("slack", False, f"auth.test failed: {error}")
    except Exception as e:
        _record("slack", False, str(e))

    # Channel access
    channel = os.environ.get("SLACK_CHANNEL", "") or os.environ.get("SLACK_CHANNEL", "")
    if not channel:
        _skip("slack-channel", "SLACK_CHANNEL / SLACK_CHANNEL not set")
        return
    channel_name = channel.lstrip("#")
    try:
        cursor = None
        found = False
        for _ in range(5):  # max 5 pages
            params: dict = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = requests.get(
                "https://slack.com/api/conversations.list",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=TIMEOUT,
            )
            data = resp.json() if resp.ok else {}
            if not data.get("ok"):
                _record("slack-channel", False, f"conversations.list error: {data.get('error', '?')}")
                return
            for ch in data.get("channels", []):
                if ch.get("name") == channel_name:
                    found = True
                    break
            if found:
                break
            cursor = data.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
        if found:
            _record("slack-channel", True, f"bot can see #{channel_name}")
        else:
            _record("slack-channel", False, f"#{channel_name} not found (bot not in channel?)")
    except Exception as e:
        _record("slack-channel", False, str(e))


# ---------------------------------------------------------------------------
# Claude (Bedrock proxy or direct API)
# ---------------------------------------------------------------------------

def check_claude() -> None:
    use_bedrock = os.environ.get("CLAUDE_CODE_USE_BEDROCK", "0") == "1"
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    bedrock_url = os.environ.get("ANTHROPIC_BEDROCK_BASE_URL", "http://ai/bedrock")

    if not use_bedrock and not api_key:
        _skip("claude", "neither CLAUDE_CODE_USE_BEDROCK=1 nor ANTHROPIC_API_KEY set")
        return

    # Derive the Aperture proxy base URL from the bedrock URL (strip /bedrock suffix if present)
    proxy_base = bedrock_url
    if proxy_base.endswith("/bedrock"):
        proxy_base = proxy_base[: -len("/bedrock")]

    from scripts.common.config import MODEL_BEDROCK_SONNET, MODEL_SONNET
    model = (
        os.environ.get("AI_MODEL")
        or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
        or (MODEL_BEDROCK_SONNET if use_bedrock else MODEL_SONNET)
    )

    if use_bedrock:
        # Check proxy reachability and list available models via /v1/models
        models_url = f"{proxy_base}/v1/models"
        try:
            resp = requests.get(models_url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                model_ids = [m.get("id", m) if isinstance(m, dict) else str(m) for m in data.get("models", data if isinstance(data, list) else [])]
                model_found = model in model_ids
                _record(
                    "claude-models",
                    True,
                    f"proxy reachable at {proxy_base} — {len(model_ids)} models, "
                    f"target model {'found' if model_found else 'NOT FOUND'}: {model}",
                )
                if not model_found and model_ids:
                    print(f"    Available models: {', '.join(model_ids[:5])}{'...' if len(model_ids) > 5 else ''}", flush=True)
            else:
                _record("claude-models", False, f"GET {models_url} => HTTP {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            _record("claude-models", False, f"cannot reach {models_url}: {e}")

        # Hit the Bedrock proxy's messages endpoint directly with a minimal prompt
        url = f"{bedrock_url}/v1/messages"
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        skip_auth = os.environ.get("CLAUDE_CODE_SKIP_BEDROCK_AUTH", "0") == "1"
        if not skip_auth:
            headers["x-api-key"] = "bedrock"
        payload = {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                text = resp.json().get("content", [{}])[0].get("text", "?").strip()
                _record("claude-bedrock", True, f"model={model} response={text!r}")
            else:
                _record("claude-bedrock", False, f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            _record("claude-bedrock", False, str(e))
    else:
        # Direct Anthropic API
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
                },
                timeout=30,
            )
            if resp.status_code == 200:
                text = resp.json().get("content", [{}])[0].get("text", "?").strip()
                _record("claude-direct", True, f"model={model} response={text!r}")
            else:
                _record("claude-direct", False, f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            _record("claude-direct", False, str(e))


# ---------------------------------------------------------------------------
# Claude CLI (subprocess)
# ---------------------------------------------------------------------------

def check_claude_cli() -> None:
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            version = result.stdout.strip() or result.stderr.strip()
            _record("claude-cli", True, f"installed: {version}")
        else:
            _record("claude-cli", False, f"exit {result.returncode}: {result.stderr.strip()[:120]}")
    except FileNotFoundError:
        _record("claude-cli", False, "claude binary not found in PATH")
    except Exception as e:
        _record("claude-cli", False, str(e))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[connectivity] checking integrations at {datetime.now(timezone.utc).isoformat()}", flush=True)
    print("", flush=True)

    check_github()
    check_jira()
    check_bitbucket()
    check_slack()
    check_claude()
    check_claude_cli()

    print("", flush=True)

    ok_count = sum(1 for r in _results if r["status"] == "OK")
    fail_count = sum(1 for r in _results if r["status"] == "FAIL")
    skip_count = sum(1 for r in _results if r["status"] == "SKIP")

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ok": ok_count,
        "fail": fail_count,
        "skip": skip_count,
        "checks": _results,
    }

    result_path = WORKSPACE_DIR / "connectivity_result.json"
    result_path.write_text(json.dumps(summary, indent=2))

    overall = "PASS" if fail_count == 0 else "FAIL"
    print(f"[connectivity] {overall} — ok={ok_count} fail={fail_count} skip={skip_count}", flush=True)
    print(json.dumps(summary))
    # Always exit 0 — failures are informational, not fatal
    sys.exit(0)


if __name__ == "__main__":
    main()
