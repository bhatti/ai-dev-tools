"""Integration tests for scripts/common/report_renderer.py.

Covers the exact failure modes that caused production issues:
- Slack mrkdwn links <url|text> must become <a> tags, never raw text
- HTML-unsafe characters in content must be escaped (no raw HTML injection)
- Tables, code blocks, lists, images must render correctly
- Self-contained CSS (no CDN links that break on private networks)
"""

import re
import pytest
from scripts.common.report_renderer import render_simple_html


# ---------------------------------------------------------------------------
# Slack mrkdwn link conversion (the user-visible regression)
# ---------------------------------------------------------------------------

class TestSlackMrkdwnLinks:
    """Slack mrkdwn <url|text> must become <a href="url">text</a> in HTML output.

    When a skill (e.g. ygs-standup) writes standup_brief.md using Slack mrkdwn
    format and run_skill.py passes that content to render_simple_html, the links
    must be rendered as clickable anchors — not as literal &lt;url|text&gt; text.
    """

    def test_bare_slack_link_converted(self):
        html = render_simple_html("Test", "<https://jira.com/browse/PROJ-1|PROJ-1>")
        assert '<a href="https://jira.com/browse/PROJ-1">PROJ-1</a>' in html
        assert "&lt;https://" not in html

    def test_slack_link_inline_in_paragraph(self):
        html = render_simple_html(
            "Test",
            "PR <https://bitbucket.org/org/repo/pull-requests/123|#123> is stale",
        )
        assert '<a href="https://bitbucket.org/org/repo/pull-requests/123">#123</a>' in html
        assert "&lt;https://" not in html

    def test_multiple_slack_links_on_same_line(self):
        html = render_simple_html(
            "Test",
            "🔴 PR <https://bitbucket.org/repo/pull/47076|#47076> "
            "(<https://jira.com/browse/CRIBL-44643|CRIBL-44643>, Holden) — 35 days open",
        )
        assert '<a href="https://bitbucket.org/repo/pull/47076">#47076</a>' in html
        assert '<a href="https://jira.com/browse/CRIBL-44643">CRIBL-44643</a>' in html
        assert "&lt;https://" not in html

    def test_slack_link_in_list_item(self):
        html = render_simple_html(
            "Test",
            "- 🔴 <https://jira.com/browse/PROJ-456|PROJ-456> blocked on Maersk",
        )
        assert '<a href="https://jira.com/browse/PROJ-456">PROJ-456</a>' in html
        assert "&lt;https://" not in html

    def test_slack_link_in_heading(self):
        html = render_simple_html(
            "Test",
            "## Action: review <https://bitbucket.org/org/repo/pull/99|PR #99>",
        )
        assert '<a href="https://bitbucket.org/org/repo/pull/99">PR #99</a>' in html
        assert "&lt;https://" not in html

    def test_slack_link_not_confused_with_html_tags(self):
        """A Slack link starting with < must not be treated as raw HTML."""
        html = render_simple_html("Test", "<https://example.com/path|Click here>")
        # Must produce an anchor, not a broken HTML element
        assert '<a href=' in html
        assert "Click here" in html
        # The original < must not leak as a raw tag
        assert "<https:" not in html

    def test_standard_markdown_link_still_works(self):
        html = render_simple_html("Test", "[Click here](https://example.com)")
        assert '<a href="https://example.com">Click here</a>' in html

    def test_both_link_styles_in_one_document(self):
        md = (
            "See [docs](https://docs.example.com) and "
            "<https://jira.com/browse/T-1|ticket T-1>."
        )
        html = render_simple_html("Test", md)
        assert '<a href="https://docs.example.com">docs</a>' in html
        assert '<a href="https://jira.com/browse/T-1">ticket T-1</a>' in html
        assert "&lt;https://" not in html


# ---------------------------------------------------------------------------
# No raw HTML injection (XSS / escaping)
# ---------------------------------------------------------------------------

class TestHTMLEscaping:
    def test_angle_brackets_in_prose_escaped(self):
        """< > in plain text must be HTML-escaped, not treated as tags."""
        html = render_simple_html("Test", "Use x < y and y > z in formulas")
        assert "&lt;" in html
        assert "&gt;" in html
        assert "<y" not in html

    def test_ampersand_escaped(self):
        html = render_simple_html("Test", "AT&T and Macy's & more")
        assert "&amp;" in html
        # Raw & must not appear outside entity references
        body = html[html.find("<body>"):html.find("</body>")]
        # Allow &amp; &lt; &gt; but no bare & not part of an entity
        assert not re.search(r"&(?!amp;|lt;|gt;|#)", body)

    def test_script_tag_in_content_escaped(self):
        """<script> in content must be escaped, never executed."""
        html = render_simple_html("Test", "bad <script>alert(1)</script> input")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_code_block_content_escaped(self):
        html = render_simple_html("Test", "```\n<script>evil()</script>\n```")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_inline_code_content_escaped(self):
        html = render_simple_html("Test", "use `<div>` element")
        assert "<div>" not in html or "<code>&lt;div&gt;</code>" in html


# ---------------------------------------------------------------------------
# Self-contained CSS (no CDN dependencies)
# ---------------------------------------------------------------------------

class TestSelfContainedCSS:
    def test_no_cdn_links(self):
        html = render_simple_html("Test", "# Hello")
        assert "cdn.jsdelivr.net" not in html
        assert "bootstrap.min.css" not in html
        assert "fonts.googleapis.com" not in html

    def test_has_inline_style(self):
        html = render_simple_html("Test", "# Hello")
        assert "<style>" in html

    def test_no_bootstrap_class_names_in_output(self):
        """Tables must not emit Bootstrap class names — CSS is self-contained."""
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        html = render_simple_html("Test", md)
        assert "table-bordered" not in html
        assert "table-striped" not in html
        assert "table-hover" not in html


# ---------------------------------------------------------------------------
# Markdown → HTML structural conversion
# ---------------------------------------------------------------------------

class TestMarkdownConversion:
    def test_headings_h1_to_h3(self):
        html = render_simple_html("Test", "# H1\n## H2\n### H3")
        assert "<h1>" in html
        assert "<h2>" in html
        assert "<h3>" in html

    def test_table_renders_as_html_table(self):
        md = "| Col A | Col B |\n|-------|-------|\n| foo   | bar   |"
        html = render_simple_html("Test", md)
        assert "<table>" in html
        assert "<th>" in html
        assert "<td>" in html
        assert "foo" in html
        assert "bar" in html

    def test_fenced_code_block(self):
        html = render_simple_html("Test", "```python\ndef hello():\n    return 42\n```")
        assert "<pre>" in html
        assert "<code" in html
        assert "def hello()" in html

    def test_unordered_list(self):
        html = render_simple_html("Test", "- item one\n- item two\n- item three")
        assert "<ul>" in html
        assert "<li>" in html
        assert "item one" in html

    def test_ordered_list(self):
        html = render_simple_html("Test", "1. first\n2. second\n3. third")
        assert "<ol>" in html
        assert "<li>" in html
        assert "first" in html

    def test_bold_text(self):
        html = render_simple_html("Test", "This is **bold** text")
        assert "<strong>bold</strong>" in html

    def test_image_tag(self):
        html = render_simple_html("Test", "![chart](reports/chart.png)")
        assert '<img src="reports/chart.png"' in html
        assert 'alt="chart"' in html

    def test_horizontal_rule(self):
        html = render_simple_html("Test", "before\n\n---\n\nafter")
        assert "<hr>" in html

    def test_inline_code(self):
        html = render_simple_html("Test", "Use `git status` command")
        assert "<code>git status</code>" in html

    def test_title_in_html_head(self):
        html = render_simple_html("My Report Title", "# Content")
        assert "<title>My Report Title</title>" in html

    def test_image_src_preserved_for_rewriting(self):
        """Relative src attributes must be preserved exactly for formicary URL rewriting.

        The formicary rewriteHTMLRefs function rewrites relative src/href paths
        so they route through the artifact download endpoint. If the path is wrong,
        images/links will 404.
        """
        html = render_simple_html("Test", "![alt](reports/chart.png)")
        # The src must be the exact path from the markdown, unchanged
        assert 'src="reports/chart.png"' in html

    def test_link_href_preserved_for_rewriting(self):
        """Relative href attributes must be preserved for formicary URL rewriting."""
        html = render_simple_html("Test", "[details](reports/detail.html)")
        assert 'href="reports/detail.html"' in html


# ---------------------------------------------------------------------------
# Full standup brief round-trip (the exact scenario that failed in production)
# ---------------------------------------------------------------------------

class TestStandupBriefRoundTrip:
    """Reproduce the production failure: standup_brief.md in Slack mrkdwn format
    passed to render_simple_html must produce a readable HTML page with no raw
    mrkdwn syntax visible.
    """

    BRIEF = """\
## 📣 Call to Action
All 4 open PRs have zero reviewers. PR <https://bitbucket.org/org/repo/pull-requests/47076|#47076>, \
<https://bitbucket.org/org/repo/pull-requests/48284|#48284>, and \
<https://bitbucket.org/org/repo/pull-requests/48239|#48239> each need a reviewer today.

## 👤 Per-Person Status
*Alice*: Working on <https://jira.com/browse/PROJ-100|PROJ-100> (Highest) and blocked on testing.

*Bob*: <https://jira.com/browse/PROJ-200|PROJ-200> In Review. PR \
<https://bitbucket.org/org/repo/pull-requests/49184|#49184> open 7 days, 0 reviewers.

## ⚠️ Risks
🔴 PR <https://bitbucket.org/org/repo/pull-requests/47076|#47076> \
(<https://jira.com/browse/PROJ-999|PROJ-999>, Alice) — 35 days open, 0 reviewers

🟡 <https://jira.com/browse/PROJ-493|PROJ-493> (Bob) — Blocked on Maersk testing

## 💬 Discussion Questions
PRs <https://bitbucket.org/org/repo/pull-requests/47076|#47076> (35d) have 0 reviewers — assign or close?
"""

    def test_no_raw_slack_links_in_output(self):
        html = render_simple_html("Standup Brief", self.BRIEF)
        assert "&lt;https://" not in html, "Raw Slack links still present as escaped entities"
        assert "<https://bitbucket" not in html, "Raw Slack links present as unescaped text"

    def test_all_links_converted_to_anchors(self):
        html = render_simple_html("Standup Brief", self.BRIEF)
        # Count <a href= occurrences — should match number of Slack links
        link_count = html.count('<a href="https://')
        assert link_count >= 8, f"Expected >=8 anchor tags, got {link_count}"

    def test_bitbucket_pr_links_clickable(self):
        html = render_simple_html("Standup Brief", self.BRIEF)
        assert '<a href="https://bitbucket.org/org/repo/pull-requests/47076">#47076</a>' in html
        assert '<a href="https://bitbucket.org/org/repo/pull-requests/48284">#48284</a>' in html

    def test_jira_issue_links_clickable(self):
        html = render_simple_html("Standup Brief", self.BRIEF)
        assert '<a href="https://jira.com/browse/PROJ-100">PROJ-100</a>' in html
        assert '<a href="https://jira.com/browse/PROJ-493">PROJ-493</a>' in html

    def test_section_headings_render(self):
        html = render_simple_html("Standup Brief", self.BRIEF)
        assert "Call to Action" in html
        assert "Per-Person Status" in html
        assert "Risks" in html
        assert "Discussion Questions" in html

    def test_html_is_valid_structure(self):
        html = render_simple_html("Standup Brief", self.BRIEF)
        assert html.startswith("<!DOCTYPE html>")
        assert "<html" in html
        assert "<head>" in html
        assert "<body>" in html
        assert "</html>" in html

    def test_no_raw_mrkdwn_syntax_visible(self):
        """Verify common mrkdwn artifacts don't appear as literal text."""
        html = render_simple_html("Standup Brief", self.BRIEF)
        # Slack links should not appear as raw syntax
        assert "|#47076>" not in html
        assert "|PROJ-100>" not in html
