"""Workflow YAML validation tests — two levels:

Level 1 (unit, no network): Parse YAML, simulate Go template rendering with
empty values, verify no dangling backslash continuations in script items.

Level 2 (integ, needs Formicary API): Upload each workflow YAML, fetch the
stored definition, and verify all script items parsed correctly.

Run unit only:
    python3 -m pytest tests/test_workflow_yamls.py::TestWorkflowYamlUnit -v

Run integ (requires FORMICARY_TOKEN and FORMICARY_URL env vars):
    python3 -m pytest tests/test_workflow_yamls.py::TestWorkflowYamlInteg -v
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest
import yaml

# ── constants ──────────────────────────────────────────────────────────────────

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "formicary" / "docs" / "examples"

# YAMLs to validate — subset covering all active AI workflows
WORKFLOW_YAMLS = [
    "ai-merge-queue.yaml",
    "ai-mq-lane.yaml",
    "ai-parallel-test.yaml",
    "ai-gate-review.yaml",
    "ai-scope-router.yaml",
    "ai-contract-test.yaml",
    "ai-gh-review.yaml",
    "ai-jira-review.yaml",
    "ai-standup-gh.yaml",
    "ai-codebase-audit.yaml",
]


def _yaml_path(name: str) -> Path:
    return EXAMPLES_DIR / name


def _render_go_templates_empty(text: str) -> str:
    """Simulate Go template rendering with all variables empty.

    Replaces {{- if ...}}...{{- end}} blocks with empty string (condition false).
    Replaces {{.Var}} with empty string.
    Replaces {{if ...}}...{{end}} blocks with empty string.

    This catches the exact class of bug where a dangling `\\` is left after
    template expansion (e.g. `{{if .Var}}--flag "..."{{end}} \\` → ` \\`).
    """
    # Remove {{- if ...}} ... {{- end}} blocks (with whitespace trim markers)
    text = re.sub(r'\{\{-?\s*if\s+[^}]+\}\}.*?\{\{-?\s*end\s*-?\}\}', '', text, flags=re.DOTALL)
    # Remove remaining {{if ...}} ... {{end}} (non-greedy, same line or multiline)
    text = re.sub(r'\{\{if\s+[^}]+\}\}.*?\{\{end\}\}', '', text, flags=re.DOTALL)
    # Replace {{.Var}} and {{.Var | filter}} references
    text = re.sub(r'\{\{[^}]+\}\}', '', text)
    return text


def _extract_scripts(task: dict) -> list[str]:
    return task.get("script") or []


# ── Level 1: unit tests (no network) ──────────────────────────────────────────

class TestWorkflowYamlUnit:
    """Validate workflow YAMLs without touching Formicary."""

    @pytest.mark.parametrize("name", WORKFLOW_YAMLS)
    def test_yaml_is_parseable(self, name: str) -> None:
        """YAML must be parseable after stripping Go templates (which confuse PyYAML)."""
        path = _yaml_path(name)
        if not path.exists():
            pytest.skip(f"{name} not found at {path}")
        rendered = _render_go_templates_empty(path.read_text())
        try:
            doc = yaml.safe_load(rendered)
        except yaml.YAMLError as exc:
            pytest.fail(f"{name}: YAML invalid after template stripping: {exc}")
        assert doc is not None, f"{name}: empty YAML"
        assert "job_type" in doc, f"{name}: missing job_type"
        assert "tasks" in doc, f"{name}: missing tasks"

    @pytest.mark.parametrize("name", WORKFLOW_YAMLS)
    def test_no_dangling_backslash_after_template_render(self, name: str) -> None:
        """After simulating empty-variable rendering, no script item should end with `\\`.

        This catches the bug that produced:
            yaml: line N: could not find expected ':'
        when Formicary stored a script item like `python -m ... \\\\n` (dangling continuation).
        """
        path = _yaml_path(name)
        if not path.exists():
            pytest.skip(f"{name} not found at {path}")

        raw = path.read_text()
        rendered = _render_go_templates_empty(raw)

        try:
            doc = yaml.safe_load(rendered)
        except yaml.YAMLError as exc:
            pytest.fail(f"{name}: rendered YAML is invalid after template expansion: {exc}")

        assert doc is not None
        for task in doc.get("tasks", []):
            task_type = task.get("task_type", "?")
            for script_item in _extract_scripts(task):
                stripped = script_item.rstrip()
                # A script item that ends with `\` after stripping is a dangling continuation
                # (means a conditional block was removed, leaving only the continuation).
                assert not stripped.endswith("\\"), (
                    f"{name} task={task_type}: script item ends with dangling `\\` after "
                    f"template rendering with empty values.\n"
                    f"Script: {script_item!r}\n"
                    f"Use the ARGS pattern instead:\n"
                    f"  ARGS=\"\"\n"
                    f'  {{{{- if .Var}}}}\n'
                    f'  ARGS="$ARGS --flag {{{{.Var}}}}"\n'
                    f'  {{{{- end}}}}\n'
                    f"  python -m scripts.foo $ARGS"
                )

    @pytest.mark.parametrize("name", WORKFLOW_YAMLS)
    def test_all_tasks_have_required_fields(self, name: str) -> None:
        path = _yaml_path(name)
        if not path.exists():
            pytest.skip(f"{name} not found at {path}")
        rendered = _render_go_templates_empty(path.read_text())
        doc = yaml.safe_load(rendered)
        for task in doc.get("tasks", []):
            assert "task_type" in task, f"{name}: task missing task_type: {task}"
            assert "method" in task, f"{name} task={task.get('task_type')}: missing method"

    @pytest.mark.parametrize("name", WORKFLOW_YAMLS)
    def test_no_fan_out_in_merge_queue(self, name: str) -> None:
        """ai-merge-queue must be purely linear — no fan_out allowed.

        fan_out requires LANE_GROUPS in task context which caused:
            fan_out.source "LANE_GROUPS" not found in job execution context
        """
        if name != "ai-merge-queue.yaml":
            pytest.skip("Only applies to ai-merge-queue.yaml")
        path = _yaml_path(name)
        if not path.exists():
            pytest.skip(f"{name} not found")
        raw = path.read_text()
        doc = yaml.safe_load(_render_go_templates_empty(raw))
        assert "fan_out" not in raw, (
            "ai-merge-queue.yaml must not contain fan_out. "
            "Use linear pipeline: collect → group → analyze → report → done"
        )
        task_types = [t["task_type"] for t in doc.get("tasks", [])]
        assert "launch-lanes" not in task_types, (
            "ai-merge-queue.yaml must not have a launch-lanes task (that was the fan_out task)"
        )


# ── Level 2: integ tests (requires Formicary API) ─────────────────────────────

def _formicary_url() -> str:
    return os.environ.get("FORMICARY_URL", "https://10.8.97.24.nip.io")


def _formicary_token() -> str:
    return os.environ.get("FORMICARY_TOKEN", "")


def _skip_if_no_formicary() -> None:
    if not _formicary_token():
        pytest.skip("FORMICARY_TOKEN not set — skipping Formicary API integ tests")


@pytest.mark.integration
class TestWorkflowYamlInteg:
    """Validate workflow YAMLs by uploading to Formicary and checking stored definitions."""

    @pytest.mark.parametrize("name", WORKFLOW_YAMLS)
    def test_upload_and_stored_scripts_have_no_dangling_backslash(self, name: str) -> None:
        """Upload workflow YAML, fetch stored definition, verify no script item ends with `\\`.

        This validates that Formicary's internal template rendering does not leave
        dangling backslash continuations that cause:
            yaml: line N: could not find expected ':'
        """
        _skip_if_no_formicary()
        import json
        import ssl
        import urllib.error
        import urllib.request

        path = _yaml_path(name)
        if not path.exists():
            pytest.skip(f"{name} not found at {path}")

        token = _formicary_token()
        base_url = _formicary_url()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/yaml"}
        # Self-signed cert in dev cluster
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # Upload
        with path.open("rb") as f:
            body = f.read()
        req = urllib.request.Request(
            f"{base_url}/api/jobs/definitions",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                assert resp.status in (200, 201), f"{name}: upload returned HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            pytest.fail(f"{name}: upload failed HTTP {exc.code}: {exc.read()[:200]}")

        # Brief pause for registration
        time.sleep(1)

        # Fetch definition
        job_type = yaml.safe_load(_render_go_templates_empty(path.read_text()))["job_type"]
        req2 = urllib.request.Request(
            f"{base_url}/api/jobs/definitions/{job_type}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req2, timeout=30, context=ctx) as resp:
            definition = json.load(resp)

        # Check every script item in every task
        for task in definition.get("tasks", []):
            task_type = task.get("task_type", "?")
            for item in task.get("script") or []:
                stripped = item.rstrip()
                assert not stripped.endswith("\\"), (
                    f"{name} task={task_type}: Formicary stored a script item ending with "
                    f"dangling `\\`. This will cause 'yaml: could not find expected :' at runtime.\n"
                    f"Stored item: {item!r}"
                )
