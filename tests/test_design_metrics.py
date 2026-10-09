"""Tests for scripts/common/design_metrics.render_design_metrics_table."""

import pytest

from scripts.common.design_metrics import render_design_metrics_table


def _full_metrics(**overrides) -> dict:
    base = {
        "srp_violations": 0,
        "dip_violations": 0,
        "adp_cycles": 0,
        "layer_violations": 0,
        "avg_instability": 0.3,
        "hotspot_coupling": False,
        "readability_score": 8,
        "isp_violations": 0,
    }
    base.update(overrides)
    return base


class TestRenderDesignMetricsTable:

    def test_empty_dict_returns_empty_list(self):
        assert render_design_metrics_table({}) == []

    def test_none_returns_empty_list(self):
        assert render_design_metrics_table(None) == []

    def test_returns_list_of_strings(self):
        lines = render_design_metrics_table(_full_metrics())
        assert isinstance(lines, list)
        assert all(isinstance(l, str) for l in lines)

    def test_section_header_present(self):
        lines = render_design_metrics_table(_full_metrics())
        assert "## Design Quality Metrics" in lines

    def test_all_eight_rows_present(self):
        lines = render_design_metrics_table(_full_metrics())
        text = "\n".join(lines)
        assert "SRP violations" in text
        assert "DIP violations" in text
        assert "Cyclic deps (ADP)" in text
        assert "Layer violations" in text
        assert "Avg instability (I)" in text
        assert "Hotspot coupling" in text
        assert "Readability score" in text
        assert "ISP / fat interface" in text

    # --- null / None field guard (the production crash scenario) ---

    def test_null_avg_instability_does_not_raise(self):
        """avg_instability: null from Claude must not crash with TypeError."""
        lines = render_design_metrics_table(_full_metrics(avg_instability=None))
        text = "\n".join(lines)
        assert "Avg instability (I)" in text
        assert "0.00" in text  # falls back to 0.0

    def test_null_srp_violations_does_not_raise(self):
        lines = render_design_metrics_table(_full_metrics(srp_violations=None))
        text = "\n".join(lines)
        assert "SRP violations" in text

    def test_null_adp_cycles_does_not_raise(self):
        lines = render_design_metrics_table(_full_metrics(adp_cycles=None))
        text = "\n".join(lines)
        assert "Cyclic deps (ADP)" in text

    def test_null_readability_score_does_not_raise(self):
        lines = render_design_metrics_table(_full_metrics(readability_score=None))
        text = "\n".join(lines)
        assert "Readability score" in text

    def test_all_fields_null_does_not_raise(self):
        """All fields explicitly null — should not crash and should render defaults."""
        dm = {k: None for k in [
            "srp_violations", "dip_violations", "adp_cycles", "layer_violations",
            "avg_instability", "hotspot_coupling", "readability_score", "isp_violations",
        ]}
        lines = render_design_metrics_table(dm)
        assert len(lines) > 0
        text = "\n".join(lines)
        assert "Design Quality Metrics" in text

    # --- signal thresholds ---

    def test_all_green_clean_codebase(self):
        lines = render_design_metrics_table(_full_metrics(readability_score=9))
        text = "\n".join(lines)
        assert "🔴" not in text
        assert "🟡" not in text

    def test_srp_zero_is_green(self):
        row = [l for l in render_design_metrics_table(_full_metrics()) if "SRP violations" in l][0]
        assert "🟢" in row

    def test_srp_one_is_yellow(self):
        row = [l for l in render_design_metrics_table(_full_metrics(srp_violations=1)) if "SRP violations" in l][0]
        assert "🟡" in row

    def test_srp_three_is_red(self):
        row = [l for l in render_design_metrics_table(_full_metrics(srp_violations=3)) if "SRP violations" in l][0]
        assert "🔴" in row

    def test_adp_zero_is_green(self):
        row = [l for l in render_design_metrics_table(_full_metrics()) if "Cyclic deps" in l][0]
        assert "🟢" in row

    def test_adp_one_is_red(self):
        row = [l for l in render_design_metrics_table(_full_metrics(adp_cycles=1)) if "Cyclic deps" in l][0]
        assert "🔴" in row

    def test_instability_below_threshold_is_green(self):
        row = [l for l in render_design_metrics_table(_full_metrics(avg_instability=0.4)) if "instability" in l][0]
        assert "🟢" in row

    def test_instability_at_boundary_is_yellow(self):
        # benchmark is < 0.5 healthy; 0.5 exactly should be yellow (boundary is exclusive)
        row = [l for l in render_design_metrics_table(_full_metrics(avg_instability=0.5)) if "instability" in l][0]
        assert "🟡" in row

    def test_instability_high_is_red(self):
        row = [l for l in render_design_metrics_table(_full_metrics(avg_instability=0.8)) if "instability" in l][0]
        assert "🔴" in row

    def test_hotspot_false_is_green(self):
        row = [l for l in render_design_metrics_table(_full_metrics(hotspot_coupling=False)) if "Hotspot" in l][0]
        assert "🟢" in row

    def test_hotspot_true_is_yellow(self):
        row = [l for l in render_design_metrics_table(_full_metrics(hotspot_coupling=True)) if "Hotspot" in l][0]
        assert "🟡" in row

    def test_readability_below_threshold_is_red(self):
        row = [l for l in render_design_metrics_table(_full_metrics(readability_score=3)) if "Readability" in l][0]
        assert "🔴" in row

    def test_readability_mid_is_yellow(self):
        row = [l for l in render_design_metrics_table(_full_metrics(readability_score=6)) if "Readability" in l][0]
        assert "🟡" in row

    def test_readability_high_is_green(self):
        row = [l for l in render_design_metrics_table(_full_metrics(readability_score=7)) if "Readability" in l][0]
        assert "🟢" in row
