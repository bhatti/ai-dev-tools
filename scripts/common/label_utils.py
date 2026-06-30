"""Label operations for GitHub and Jira.

GitHub: uses the `gh` CLI.
Jira:   uses the Jira REST API (via scripts.common.jira_api).
"""

import json
import subprocess
import sys

from scripts.common.jira_api import (
    add_comment as jira_add_comment_api,
    transition_label as jira_transition_label_api,
    add_label as jira_add_label_api,
    remove_label as jira_remove_label_api,
)


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


# --- GitHub ---
# Uses REST API via `gh api` — avoids GraphQL mutations that require elevated
# fine-grained PAT scopes (removeLabelsFromLabelable). REST label endpoints
# work with standard Issues: Read and write permissions.

def gh_add_label(org: str, repo: str, issue_num: int | str, label: str) -> None:
    """Add a label via REST API. Raises RuntimeError on failure."""
    result = _run(
        [
            "gh", "api", "--method", "POST",
            f"repos/{org}/{repo}/issues/{issue_num}/labels",
            "-f", f"labels[]={label}",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not add label '{label}' to #{issue_num}: {result.stderr.strip()}"
        )


def gh_remove_label(org: str, repo: str, issue_num: int | str, label: str) -> None:
    """Remove a label via REST API. Non-fatal if label not present (404)."""
    import urllib.parse
    encoded = urllib.parse.quote(label, safe="")
    result = _run(
        [
            "gh", "api", "--method", "DELETE",
            f"repos/{org}/{repo}/issues/{issue_num}/labels/{encoded}",
        ],
        check=False,
    )
    # 404 = label wasn't on the issue — that's fine
    if result.returncode != 0 and "404" not in result.stderr and "not found" not in result.stderr.lower():
        raise RuntimeError(
            f"Could not remove label '{label}' from #{issue_num}: {result.stderr.strip()}"
        )


def gh_transition_label(
    org: str, repo: str, issue_num: int | str, from_label: str, to_label: str
) -> None:
    """Remove one label and add another. Raises RuntimeError on any failure."""
    gh_remove_label(org, repo, issue_num, from_label)
    gh_add_label(org, repo, issue_num, to_label)


def gh_has_label(org: str, repo: str, issue_num: int | str, label: str) -> bool:
    """Return True if the issue has the given label (REST API)."""
    result = _run(
        ["gh", "api", f"repos/{org}/{repo}/issues/{issue_num}/labels"],
        check=False,
    )
    if result.returncode != 0:
        return False
    labels = json.loads(result.stdout or "[]")
    return any(l["name"] == label for l in labels)


# --- Jira (delegates to REST API) ---

def jira_add_label(config: dict, issue_key: str, label: str) -> None:
    """Add a label to a Jira issue."""
    jira_add_label_api(config, issue_key, label)


def jira_remove_label(config: dict, issue_key: str, label: str) -> None:
    """Remove a label from a Jira issue."""
    jira_remove_label_api(config, issue_key, label)


def jira_transition_label(config: dict, issue_key: str, from_label: str, to_label: str) -> None:
    """Remove one Jira label and add another atomically via REST API."""
    jira_transition_label_api(config, issue_key, from_label, to_label)


def jira_add_comment(config: dict, issue_key: str, body: str) -> None:
    """Post a comment on a Jira issue."""
    jira_add_comment_api(config, issue_key, body)
