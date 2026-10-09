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

    # Use `or` fallback (not just .get default) so explicit null values from Claude also get replaced
    srp = design_metrics.get("srp_violations") or 0
    dip = design_metrics.get("dip_violations") or 0
    adp = design_metrics.get("adp_cycles") or 0
    layer = design_metrics.get("layer_violations") or 0
    inst = float(design_metrics.get("avg_instability") or 0.0)
    hotspot = design_metrics.get("hotspot_coupling") or False
    readability = design_metrics.get("readability_score") or 0
    isp = design_metrics.get("isp_violations") or 0

    # Benchmark: < 0.5 healthy — at exactly 0.5 use yellow (boundary is exclusive)
    inst_sig = "🟢" if inst < 0.5 else ("🟡" if inst < 0.7 else "🔴")
    read_sig = "🟢" if readability >= 7 else ("🟡" if readability >= 5 else "🔴")

    return [
        "## Design Quality Metrics",
        "",
        "| Principle | Score | Benchmark | Signal | Notes |",
        "|-----------|-------|-----------|--------|-------|",
        f"| SRP violations | {srp} | 0 ideal | {'🟢' if srp == 0 else ('🟡' if srp <= 2 else '🔴')} | {'none found' if srp == 0 else f'{srp} confirmed'} |",
        f"| DIP violations | {dip} | 0 ideal | {'🟢' if dip == 0 else ('🟡' if dip == 1 else '🔴')} | {'none found' if dip == 0 else f'{dip} confirmed'} |",
        f"| Cyclic deps (ADP) | {adp} | 0 | {'🟢' if adp == 0 else '🔴'} | {'none detected' if adp == 0 else f'{adp} cycles'} |",
        f"| Layer violations | {layer} | 0 ideal | {'🟢' if layer == 0 else ('🟡' if layer <= 2 else '🔴')} | {'none' if layer == 0 else f'{layer} violations'} |",
        f"| Avg instability (I) | {inst:.2f} | < 0.5 (core) | {inst_sig} | {'N/A' if inst == 0 else f'{inst:.2f} mean'} |",
        f"| Hotspot coupling | {'1 file' if hotspot else '0'} | 0 ideal | {'🟡' if hotspot else '🟢'} | {'changed file is hotspot' if hotspot else 'none'} |",
        f"| Readability score | {readability}/10 | ≥ 7 | {read_sig} | {'all good' if readability >= 7 else 'see findings'} |",
        f"| ISP / fat interface | {isp} | 0 ideal | {'🟢' if isp == 0 else ('🟡' if isp == 1 else '🔴')} | {'none found' if isp == 0 else f'{isp} violations'} |",
        "",
    ]
