"""Write .ygs/tracker.yml and set JIRA_AUTH based on environment variables.

Run as a script at job startup:
    python -m scripts.common.setup_tracker

Reads from env:
    JIRA_BASE_URL, JIRA_PROJECT, JIRA_EMAIL, JIRA_API_TOKEN
    GH_ORG, GH_REPO, STANDUP_TEAM_MEMBERS
    WORKSPACE_DIR (default /workspace)

Writes:
    $WORKSPACE_DIR/.ygs/tracker.yml
    Prints: export JIRA_AUTH=...  (caller can eval if needed; also sets os.environ)
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    workspace = os.environ.get("WORKSPACE_DIR", "/workspace")
    ygs_dir = Path(workspace) / ".ygs"
    ygs_dir.mkdir(parents=True, exist_ok=True)

    jira_url = os.environ.get("JIRA_BASE_URL", "")
    jira_project = os.environ.get("JIRA_PROJECT", "")
    jira_email = os.environ.get("JIRA_EMAIL", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")
    gh_org = os.environ.get("GH_ORG", "")
    gh_repo = os.environ.get("GH_REPO", "")
    team = os.environ.get("STANDUP_TEAM_MEMBERS", "")

    # Compute and export JIRA_AUTH
    if jira_email and jira_token:
        auth = base64.b64encode(f"{jira_email}:{jira_token}".encode()).decode()
        os.environ["JIRA_AUTH"] = auth
        print(f"export JIRA_AUTH={auth}", flush=True)

    # Write tracker.yml
    tracker_file = ygs_dir / "tracker.yml"
    if jira_url and jira_project:
        tracker_file.write_text(
            f"tracker: jira\n"
            f"project: {jira_project}\n"
            f"board: {jira_project}\n"
            f"base_url: {jira_url}\n"
            f"team: {team}\n"
        )
        print(f"[setup_tracker] wrote jira tracker: {tracker_file}", flush=True)
    elif gh_org and gh_repo:
        tracker_file.write_text(
            f"tracker: github\n"
            f"org: {gh_org}\n"
            f"repo: {gh_repo}\n"
            f"team: {team}\n"
        )
        print(f"[setup_tracker] wrote github tracker: {tracker_file}", flush=True)
    else:
        print("[setup_tracker] no tracker configured (JIRA_BASE_URL+JIRA_PROJECT or GH_ORG+GH_REPO required)", flush=True)

    # Authenticate gh CLI if GH_TOKEN is set
    gh_token = os.environ.get("GH_TOKEN", "")
    if gh_token:
        try:
            result = subprocess.run(
                ["gh", "auth", "login", "--with-token"],
                input=gh_token, text=True, capture_output=True
            )
            if result.returncode == 0:
                print("[setup_tracker] gh auth login: ok", flush=True)
            else:
                print(f"[setup_tracker] gh auth login failed (non-fatal): {result.stderr.strip()}", flush=True)
        except FileNotFoundError:
            pass  # gh not installed — not required for all skills


if __name__ == "__main__":
    main()
