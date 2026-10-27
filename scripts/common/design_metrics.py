# SPDX-License-Identifier: LGPL-2.1-or-later
"""Shared renderer for the canonical 8-row Design Quality Metrics table.

Used by mq/report.py and review/post_findings.py so both produce identical output.
"""

from __future__ import annotations


def render_design_metrics_table(design_metrics: dict) -> list[str]:
    """Return Markdown lines for the Design Quality Metrics section.

    Returns an empty list when *design_metrics* is empty or falsy.
    The caller is responsible for inserting these lines at the right position
    in the report (before findings per convention).
    """
    if not design_metrics:
        return []

    def num(key: str):
        """The reported number, or None: a null or missing value is "not assessed", never a clean zero."""
        value = design_metrics.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def row(name: str, key: str, benchmark: str, fmt, signal, note) -> str:
        value = num(key)
        if value is None:
            return f"| {name} | — | {benchmark} | — | not assessed |"
        return f"| {name} | {fmt(value)} | {benchmark} | {signal(value)} | {note(value)} |"

    hotspot = design_metrics.get("hotspot_coupling")   # a flag: true / false / null
    hotspot_row = ("| Hotspot coupling | — | 0 ideal | — | not assessed |" if hotspot is None else
                   f"| Hotspot coupling | {'1 file' if hotspot else '0'} | 0 ideal | {'🟡' if hotspot else '🟢'} | {'changed file is hotspot' if hotspot else 'none'} |")
    ladder = lambda warn: (lambda v: "🟢" if v == 0 else ("🟡" if v <= warn else "🔴"))   # noqa: E731
    return [
        "## Design Quality Metrics",
        "",
        "| Principle | Score | Benchmark | Signal | Notes |",
        "|-----------|-------|-----------|--------|-------|",
        row("SRP violations", "srp_violations", "0 ideal", str, ladder(2), lambda v: "none found" if v == 0 else f"{v} confirmed"),
        row("DIP violations", "dip_violations", "0 ideal", str, ladder(1), lambda v: "none found" if v == 0 else f"{v} confirmed"),
        row("Cyclic deps (ADP)", "adp_cycles", "0", str, lambda v: "🟢" if v == 0 else "🔴", lambda v: "none detected" if v == 0 else f"{v} cycles"),
        row("Layer violations", "layer_violations", "0 ideal", str, ladder(2), lambda v: "none" if v == 0 else f"{v} violations"),
        # < 0.5 healthy: exactly 0.5 is yellow (the boundary is exclusive)
        row("Avg instability (I)", "avg_instability", "< 0.5 (core)", lambda v: f"{v:.2f}", lambda v: "🟢" if v < 0.5 else ("🟡" if v < 0.7 else "🔴"),
            lambda v: "N/A" if v == 0 else f"{v:.2f} mean"),
        hotspot_row,
        row("Readability score", "readability_score", "≥ 7", lambda v: f"{v}/10", lambda v: "🟢" if v >= 7 else ("🟡" if v >= 5 else "🔴"),
            lambda v: "all good" if v >= 7 else "see findings"),
        row("ISP / fat interface", "isp_violations", "0 ideal", str, ladder(1), lambda v: "none found" if v == 0 else f"{v} violations"),
        "",
    ]
