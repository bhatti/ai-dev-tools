# SPDX-License-Identifier: LGPL-2.1-or-later
"""Run options for the PR audit that must survive across task pods.

Slack flags (``--dry-run``, ``--reviewers``) are parsed only in the ``audit-prs`` pod, but
``plan-skill-updates`` and ``create-skill-pr`` run in separate pods. ``audit-prs`` writes
``reports/run_options.json`` (a task artifact the later tasks already read) and later tasks
call :func:`load`. Env vars (``PR_AUDIT_DRY_RUN`` / ``PR_AUDIT_REVIEWERS``, set from job
variables) are the fallback so API-submitted jobs work without Slack.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

_FILENAME = "run_options.json"
_FALSEY = {"", "0", "false", "no", "off", "<no value>"}


@dataclass(frozen=True)
class AuditOptions:
    """Options shared by every task of one audit run."""

    dry_run: bool = False
    reviewers: tuple[str, ...] = ()


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() not in _FALSEY


def parse_reviewers(value: str | None) -> tuple[str, ...]:
    """Split a comma list into unique names (case-insensitive), preserving first spelling."""
    seen: dict[str, str] = {}
    for raw in (value or "").split(","):
        name = raw.strip()
        if not name or name == "<no value>":
            continue
        seen.setdefault(name.lower(), name)
    return tuple(seen.values())


def resolve(config: dict, slack_flags: dict) -> AuditOptions:
    """Slack flag > config/env > default."""
    reviewers = parse_reviewers(slack_flags.get("reviewers")) or parse_reviewers(config.get("PR_AUDIT_REVIEWERS"))
    dry_run = bool(slack_flags.get("dry_run")) or _truthy(config.get("PR_AUDIT_DRY_RUN"))
    return AuditOptions(dry_run=dry_run, reviewers=reviewers)


def write(reports_dir: Path, opts: AuditOptions, unmatched: list[str] | tuple[str, ...] = ()) -> Path:
    """Persist options for downstream tasks; ``unmatched`` = requested reviewers with no PR activity."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / _FILENAME
    path.write_text(json.dumps({**asdict(opts), "reviewers": list(opts.reviewers), "unmatched_reviewers": list(unmatched)}, indent=2),
                    encoding="utf-8")
    return path


def load_unmatched(reports_dir: Path) -> list[str]:
    try:
        return list(json.loads((reports_dir / _FILENAME).read_text(encoding="utf-8")).get("unmatched_reviewers") or [])
    except (OSError, json.JSONDecodeError):
        return []


def load(reports_dir: Path, config: dict) -> AuditOptions:
    """Read the artifact; env wins for dry_run (either source can force a dry run, never the reverse)."""
    env = resolve(config, {})
    try:
        data = json.loads((reports_dir / _FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return env
    return AuditOptions(
        dry_run=bool(data.get("dry_run")) or env.dry_run,
        reviewers=parse_reviewers(",".join(data.get("reviewers") or [])) or env.reviewers,
    )
