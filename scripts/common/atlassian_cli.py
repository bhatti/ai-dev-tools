"""Write the Atlassian CLI configs (jira-cli and acli) from the job environment.

Formicary overrides the container ENTRYPOINT, so entrypoint.sh never runs in task pods; skills that shell out to
``acli`` (profiles ``jira`` and ``bitbucket``) or ``jira`` would find no config. Both the entrypoint and
``run_skill`` call :func:`write_cli_configs`, so there is one definition of the files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _write_private(path: Path, text: str) -> None:
    """Write a file that holds a token, readable by the owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def write_cli_configs(config: dict) -> list[Path]:
    """Create ``~/.config/jira/.config.yml`` and the ``jira`` / ``bitbucket`` acli profiles; returns files written.

    Needs ``JIRA_BASE_URL``; without it nothing is written (GitHub-only runs). Existing configuration is never
    replaced: a profile or file that is already there (a developer's own login) is kept, only missing pieces are
    added. The ``bitbucket`` profile authenticates with the Bitbucket user/token, falling back to the Jira email/token.
    """
    url = config.get("JIRA_BASE_URL", "")
    if not url:
        return []
    home = Path.home()
    email = config.get("JIRA_EMAIL", "")
    token = config.get("JIRA_API_TOKEN", "")
    project = config.get("JIRA_PROJECT", "")
    written: list[Path] = []

    jira_cfg = home / ".config" / "jira" / ".config.yml"
    if not jira_cfg.exists():
        # json.dumps gives YAML-safe quoted scalars
        _write_private(jira_cfg, "".join(f"{k}: {json.dumps(v)}\n" for k, v in
                                         {"installation": "cloud", "base-url": url, "login": email, "project": project}.items()))
        written.append(jira_cfg)

    wanted = {
        "jira": {"name": "jira", "atlassian_url": url, "email": email, "api_token": token, "defaults": {"project": project}},
        "bitbucket": {
            "name": "bitbucket", "atlassian_url": url,
            "email": config.get("BITBUCKET_USERNAME") or config.get("BB_USERNAME") or email,
            "api_token": config.get("BITBUCKET_TOKEN") or config.get("BB_TOKEN") or token,
            "defaults": {"workspace": config.get("BITBUCKET_WORKSPACE") or config.get("BB_WORKSPACE", "")},
        },
    }
    acli_cfg = home / ".config" / "acli" / "config.json"
    try:
        current = json.loads(acli_cfg.read_text(encoding="utf-8")) if acli_cfg.exists() else {}
    except (OSError, ValueError):
        return written  # unreadable existing config: leave it alone rather than risk destroying it
    profiles = current.setdefault("profiles", {})
    missing = {name: prof for name, prof in wanted.items() if name not in profiles}
    if missing or not acli_cfg.exists():
        profiles.update(missing)
        current.setdefault("default_profile", "jira")
        _write_private(acli_cfg, json.dumps(current, indent=2))
        written.append(acli_cfg)
    return written
