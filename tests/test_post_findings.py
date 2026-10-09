"""Tests for scripts/review/post_findings.py — render_report_md with design_metrics."""

import pytest

from scripts.review.post_findings import render_report_md


class TestRenderReportMdDesignMetrics:
    """Design Quality Metrics table appears in render_report_md when design_metrics is present."""

    def _base_findings(self, **kw) -> dict:
        defaults = {
            "pr_url": "https://github.com/org/myapp/pull/42",
            "verdict": "REQUEST_CHANGES",
            "summary": "Found design issues.",
            "findings": [],
        }
        defaults.update(kw)
        return defaults

    def test_no_design_metrics_no_table(self):
        findings = self._base_findings()
        md = render_report_md(findings)
        assert "Design Quality Metrics" not in md

    def test_design_metrics_section_present(self):
        findings = self._base_findings(design_metrics={
            "srp_violations": 0, "dip_violations": 0, "adp_cycles": 0,
            "layer_violations": 0, "avg_instability": 0.3,
            "hotspot_coupling": False, "readability_score": 8, "isp_violations": 0,
        })
        md = render_report_md(findings)
        assert "## Design Quality Metrics" in md

    def test_design_metrics_table_columns(self):
        findings = self._base_findings(design_metrics={
            "srp_violations": 1, "dip_violations": 0, "adp_cycles": 2,
            "layer_violations": 0, "avg_instability": 0.65,
            "hotspot_coupling": True, "readability_score": 6, "isp_violations": 1,
        })
        md = render_report_md(findings)
        assert "| Principle | Score | Benchmark | Signal | Notes |" in md
        assert "SRP violations" in md
        assert "DIP violations" in md
        assert "Cyclic deps (ADP)" in md
        assert "Layer violations" in md
        assert "Avg instability (I)" in md
        assert "Hotspot coupling" in md
        assert "Readability score" in md
        assert "ISP / fat interface" in md

    def test_srp_violation_red_signal(self):
        findings = self._base_findings(design_metrics={
            "srp_violations": 5, "dip_violations": 0, "adp_cycles": 0,
            "layer_violations": 0, "avg_instability": 0.0,
            "hotspot_coupling": False, "readability_score": 8, "isp_violations": 0,
        })
        md = render_report_md(findings)
        # 5 SRP violations → 🔴
        lines = [l for l in md.splitlines() if "SRP violations" in l]
        assert len(lines) == 1
        assert "🔴" in lines[0]

    def test_all_green_signals(self):
        findings = self._base_findings(design_metrics={
            "srp_violations": 0, "dip_violations": 0, "adp_cycles": 0,
            "layer_violations": 0, "avg_instability": 0.2,
            "hotspot_coupling": False, "readability_score": 9, "isp_violations": 0,
        })
        md = render_report_md(findings)
        # All 8 rows should have 🟢 (hotspot_coupling=False → 🟢, readability=9 → 🟢)
        dm_lines = md.split("## Design Quality Metrics")[1].split("##")[0]
        assert dm_lines.count("🔴") == 0
        assert dm_lines.count("🟡") == 0

    def test_adp_cycles_red_signal(self):
        findings = self._base_findings(design_metrics={
            "srp_violations": 0, "dip_violations": 0, "adp_cycles": 3,
            "layer_violations": 0, "avg_instability": 0.0,
            "hotspot_coupling": False, "readability_score": 8, "isp_violations": 0,
        })
        md = render_report_md(findings)
        lines = [l for l in md.splitlines() if "Cyclic deps" in l]
        assert len(lines) == 1
        assert "🔴" in lines[0]

    def test_hotspot_coupling_yellow_signal(self):
        findings = self._base_findings(design_metrics={
            "srp_violations": 0, "dip_violations": 0, "adp_cycles": 0,
            "layer_violations": 0, "avg_instability": 0.0,
            "hotspot_coupling": True, "readability_score": 8, "isp_violations": 0,
        })
        md = render_report_md(findings)
        lines = [l for l in md.splitlines() if "Hotspot coupling" in l]
        assert len(lines) == 1
        assert "🟡" in lines[0]

    def test_readability_score_red(self):
        findings = self._base_findings(design_metrics={
            "srp_violations": 0, "dip_violations": 0, "adp_cycles": 0,
            "layer_violations": 0, "avg_instability": 0.0,
            "hotspot_coupling": False, "readability_score": 3, "isp_violations": 0,
        })
        md = render_report_md(findings)
        lines = [l for l in md.splitlines() if "Readability score" in l]
        assert len(lines) == 1
        assert "🔴" in lines[0]

    def test_instability_yellow(self):
        findings = self._base_findings(design_metrics={
            "srp_violations": 0, "dip_violations": 0, "adp_cycles": 0,
            "layer_violations": 0, "avg_instability": 0.6,
            "hotspot_coupling": False, "readability_score": 8, "isp_violations": 0,
        })
        md = render_report_md(findings)
        lines = [l for l in md.splitlines() if "Avg instability" in l]
        assert len(lines) == 1
        assert "🟡" in lines[0]

    def test_design_metrics_before_findings(self):
        """Design Quality Metrics section should appear before the Findings section."""
        findings = self._base_findings(
            design_metrics={"srp_violations": 1, "dip_violations": 0, "adp_cycles": 0,
                            "layer_violations": 0, "avg_instability": 0.4,
                            "hotspot_coupling": False, "readability_score": 7, "isp_violations": 0},
            findings=[{"severity": "MEDIUM", "title": "test finding", "file": "a.py"}],
        )
        md = render_report_md(findings)
        dm_pos = md.index("## Design Quality Metrics")
        findings_pos = md.index("## Findings")
        assert dm_pos < findings_pos

    def test_findings_still_rendered_with_design_metrics(self):
        """Findings section is rendered even when design_metrics is also present."""
        findings = self._base_findings(
            design_metrics={"srp_violations": 0, "dip_violations": 0, "adp_cycles": 0,
                            "layer_violations": 0, "avg_instability": 0.3,
                            "hotspot_coupling": False, "readability_score": 8, "isp_violations": 0},
            findings=[{"severity": "HIGH", "title": "missing interface", "file": "svc.py", "line": 42,
                       "description": "Service couples to DB directly.", "fix": "Use repository interface."}],
        )
        md = render_report_md(findings)
        assert "## Findings" in md
        assert "missing interface" in md
        assert "Use repository interface." in md
