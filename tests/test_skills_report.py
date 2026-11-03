"""Tests for scripts/analyze/skills_report.py and write_markdown_report."""

from scripts.analyze.skills_report import classify_path, render_skills_report_md
from scripts.common.report_renderer import write_markdown_report

FILES = [
    {"path": ".claude/skills/review-knowledge/checklists/security.md", "action": "create",
     "description": "review-knowledge: checklists/security.md", "content": "# Security\n\n- [ ] Check tenant — Why: leak\n"},
    {"path": ".claude/skills/gotchas/pagination.md", "action": "update", "description": "pagination edge cases",
     "content": "```python\nprint(1)\n```\ntext\n"},
    {"path": "docs/notes.md", "action": "create", "description": "notes", "content": "plain"},
]


class TestClassify:
    def test_kinds(self):
        assert classify_path(FILES[0]["path"]) == "review-knowledge"
        assert classify_path(FILES[1]["path"]) == "skill"
        assert classify_path("docs/notes.md") == "doc"


class TestRender:
    def test_summary_table_status_and_full_content(self):
        md = render_skills_report_md(FILES, dry_run=False, pr_url="https://x/pull/7", repo="o/r")
        assert "https://x/pull/7" in md
        assert "| `.claude/skills/review-knowledge/checklists/security.md` | review-knowledge | create |" in md
        assert "- [ ] Check tenant — Why: leak" in md

    def test_dry_run_banner_and_no_pr_link(self):
        md = render_skills_report_md(FILES, dry_run=True, pr_url="", repo="o/r")
        assert "dry run" in md.lower() and "no pr" in md.lower()

    def test_embedded_code_fence_cannot_break_out_of_the_block(self):
        md = render_skills_report_md(FILES, dry_run=True, pr_url="", repo="o/r")
        fence_lines = [ln for ln in md.splitlines() if ln.startswith(("```", "~~~"))]
        assert len(fence_lines) % 2 == 0 and all(ln.startswith("~~~") for ln in fence_lines)  # only our own fences

    def test_empty(self):
        assert "No files" in render_skills_report_md([], dry_run=False, pr_url="", repo="o/r")


class TestWriteMarkdownReport:
    def test_writes_md_and_html_and_overwrites(self, tmp_path):
        write_markdown_report(tmp_path, "r", "Title", "# Heading\n\nfirst")
        write_markdown_report(tmp_path, "r", "Title", "# Heading\n\nsecond")
        assert (tmp_path / "r.md").read_text().endswith("second")
        assert "second" in (tmp_path / "r.html").read_text() and "<h1>" in (tmp_path / "r.html").read_text()
