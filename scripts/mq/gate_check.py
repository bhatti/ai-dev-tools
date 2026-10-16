"""Gate decision for a reviewed PR: read-only, writes ``gate_result.json`` and task-context keys.

Needs approval when the risk score reaches the threshold or the AI review found a CRITICAL finding. Findings come
from ``findings.json`` (the review's full list; ``review_result.json`` only holds the status line).

Usage: python -m scripts.mq.gate_check   (env: WORKSPACE_DIR, APPROVAL_THRESHOLD)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from scripts.common.severity import count_findings


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_findings(workspace: Path) -> list[dict] | None:
    """The review's findings, or None when no readable findings file exists (distinct from an empty list)."""
    for candidate in (workspace / "reports" / "findings.json", workspace / "findings.json"):
        data = _read(candidate)
        if data is not None and isinstance(data.get("findings"), list) and data.get("findings_available", True):
            return data["findings"]
    return None


def compute_gate(risk: dict, findings: list[dict] | None, threshold: int) -> dict:
    """Gate decision. Without readable findings the review is unknown, so approval is required (never "safe")."""
    counts = count_findings(findings)
    score = risk["score"]
    has_critical = counts["critical"] > 0
    unavailable = findings is None
    needs_approval = score >= threshold or has_critical or unavailable
    if has_critical:
        reason = "critical findings"
    elif unavailable:
        reason = "review findings unavailable"
    elif needs_approval:
        reason = f"risk score {score} >= {threshold}"
    else:
        reason = f"risk score {score} < {threshold}"
    return {
        "risk_score": score, "risk_tier": risk["tier"], "has_critical_findings": has_critical, "findings_available": not unavailable,
        "findings_count": counts["total"], "critical_count": counts["critical"], "high_count": counts["high"],
        "needs_approval": needs_approval, "reason": reason,
    }


def main() -> int:
    workspace = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
    risk = _read(workspace / "risk_score.json")
    if not risk or "score" not in risk or "tier" not in risk:
        print("ERROR: risk_score.json is missing or incomplete", file=sys.stderr)
        return 1
    try:
        threshold = int(os.environ.get("APPROVAL_THRESHOLD", "70"))
    except ValueError:
        print("ERROR: APPROVAL_THRESHOLD must be an integer", file=sys.stderr)
        return 1
    gate = compute_gate(risk, load_findings(workspace), threshold)
    (workspace / "gate_result.json").write_text(json.dumps(gate, indent=2), encoding="utf-8")
    label = "needs-approval" if gate["needs_approval"] else "safe-to-merge"
    print(f"Gate: {label} — {gate['reason']}")
    print(f"::add-task-context GATE_LABEL::{label}")
    print(f"::add-task-context GATE_REASON::{gate['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
