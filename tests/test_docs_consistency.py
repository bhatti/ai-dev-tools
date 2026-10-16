"""Docs and examples must not mention names that were removed or renamed.

Scans README/docs/skills/workflow YAML/routes/scripts of the three sibling checkouts (skipped when a checkout is
absent). Add a name to RETIRED when it is renamed; the test then finds every stale mention.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKSPACE = Path(__file__).resolve().parents[2]
REPOS = {name: WORKSPACE / name for name in ("ai-dev-tools", "formicary", "you-got-skills")}
SUFFIXES = {".md", ".yaml", ".yml", ".json", ".sh", ".py", ".go", ".txt", ".env"}
SKIP_DIRS = {".git", "node_modules", "vendor", "__pycache__", ".pytest_cache", "target", "tests", "archived_docs", ".claude"}
SKIP_FILES = {"PROJECT_TRACKER.md", "settings.local.json", "test_docs_consistency.py"}

# retired name -> what replaced it
RETIRED = {
    r"\bgate-review\b": "pr-risk",
    r"\breview-gate\b": "pr-risk",
    r"\bygs-gate-review\b": "ygs-pr-risk",
    r"\bai-gate-review\b": "ai-pr-risk",
    r"\bmodels\.env\b": "ai-dev-tools scripts/common/models.json",
    r"Anthropic(?:Sonnet|Opus|Haiku)Model|AnthropicComplexity\w*Model": "scripts/common/models.json",
    r"--mode\s+(?:verify|regression)\b(?!\s*\))": "a baseline flag selects regression mode (ygs-qa has no --mode)",
}
# Lines that talk about the retirement itself (migration notes) are allowed to name the old thing.
ALLOW_LINE = re.compile(r"retired|no longer|renamed|migration|unknown command|scripts\.qa\.report", re.I)


def _files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in SUFFIXES and not (set(path.relative_to(root).parts) & SKIP_DIRS) and path.name not in SKIP_FILES:
            yield path


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_no_stale_names(repo):
    root = REPOS[repo]
    if not root.is_dir():
        pytest.skip(f"{repo} checkout not found")
    hits = []
    for path in _files(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            for pattern, replacement in RETIRED.items():
                if pattern.startswith("--mode") and path.suffix == ".py":
                    continue   # scripts.qa.report has its own --mode switch; only user-facing docs must not use it for ygs-qa
                if re.search(pattern, line) and not ALLOW_LINE.search(line):
                    hits.append(f"{path.relative_to(WORKSPACE)}:{n}: '{re.search(pattern, line).group(0)}' -> use {replacement}")
    assert not hits, "stale names in docs/examples:\n" + "\n".join(hits[:40])
