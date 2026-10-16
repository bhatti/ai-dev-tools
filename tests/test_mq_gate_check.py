"""Tests for scripts/mq/gate_check.py (run as the workflow runs it: a subprocess with WORKSPACE_DIR)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def run(ws: Path, threshold="70"):
    env = {**os.environ, "WORKSPACE_DIR": str(ws), "APPROVAL_THRESHOLD": threshold, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-m", "scripts.mq.gate_check"], capture_output=True, text=True, env=env, cwd=ROOT)


def write(ws: Path, risk=None, findings=None, where="reports/findings.json"):
    ws.mkdir(parents=True, exist_ok=True)
    if risk is not None:
        (ws / "risk_score.json").write_text(json.dumps(risk))
    if findings is not None:
        target = ws / where
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"findings": findings}))


def gate(ws: Path) -> dict:
    return json.loads((ws / "gate_result.json").read_text())


def test_critical_finding_needs_approval_even_with_a_low_score(tmp_path):
    write(tmp_path, {"score": 10, "tier": "LOW"}, [{"severity": "CRITICAL"}, {"severity": "HIGH"}, {"severity": "LOW"}])
    r = run(tmp_path)
    g = gate(tmp_path)
    assert r.returncode == 0 and g["needs_approval"] and g["has_critical_findings"] and g["reason"] == "critical findings"
    assert (g["findings_count"], g["critical_count"], g["high_count"]) == (3, 1, 1)
    assert "::add-task-context GATE_LABEL::needs-approval" in r.stdout


def test_lowercase_severity_from_older_reviews_still_counts(tmp_path):
    write(tmp_path, {"score": 10, "tier": "LOW"}, [{"severity": "critical"}])
    run(tmp_path)
    assert gate(tmp_path)["has_critical_findings"] is True


@pytest.mark.parametrize("score,threshold,needs", [(69, "70", False), (70, "70", True), (90, "70", True)])
def test_score_threshold(tmp_path, score, threshold, needs):
    write(tmp_path, {"score": score, "tier": "X"}, [])
    r = run(tmp_path, threshold)
    assert gate(tmp_path)["needs_approval"] is needs
    assert ("needs-approval" if needs else "safe-to-merge") in r.stdout


def test_findings_next_to_the_workspace_root_are_found(tmp_path):
    write(tmp_path, {"score": 1, "tier": "LOW"}, [{"severity": "CRITICAL"}], where="findings.json")
    run(tmp_path)
    assert gate(tmp_path)["critical_count"] == 1


def test_an_empty_findings_list_with_a_low_score_is_safe(tmp_path):
    write(tmp_path, {"score": 1, "tier": "LOW"}, [])
    r = run(tmp_path)
    assert gate(tmp_path)["needs_approval"] is False and gate(tmp_path)["findings_available"] is True and "safe-to-merge" in r.stdout


@pytest.mark.parametrize("content", [None, "{not json", "[]", '{"findings": null}', '{"verdict": "COMMENT"}',
                                     '{"verdict": "COMMENT", "findings": [], "findings_available": false, "summary": "Review completed."}'])
def test_missing_or_unreadable_findings_never_count_as_safe(tmp_path, content):
    write(tmp_path, {"score": 1, "tier": "LOW"})
    if content is not None:
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "findings.json").write_text(content)
    r = run(tmp_path)
    g = gate(tmp_path)
    assert g["needs_approval"] is True and g["findings_available"] is False and g["reason"] == "review findings unavailable"
    assert "needs-approval" in r.stdout


def test_missing_risk_score_fails_loudly(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    r = run(tmp_path)
    assert r.returncode == 1 and "risk_score.json" in r.stderr and not (tmp_path / "gate_result.json").exists()


def test_bad_threshold_fails(tmp_path):
    write(tmp_path, {"score": 1, "tier": "LOW"}, [])
    assert run(tmp_path, "abc").returncode == 1
