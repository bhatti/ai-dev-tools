"""Deployment pipeline simulation formulas.

Pure-function module — no I/O, no env vars. All formulas canonical here;
report.py and the Streamlit simulator both consume them.

Based on Joe Magerramov's "Valley of Calm" model extended with:
- Batch release trains (daily/weekly vs CD)
- Rollback feasibility (stacked releases → roll-forward trap)
- Deployment maturity scoring (7 dimensions)
- Calamity threshold (the "red line")
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Deployment maturity dimensions — weights and tier boundaries
# ---------------------------------------------------------------------------

MATURITY_DIMENSIONS: dict[str, dict[str, Any]] = {
    "automated_testing":  {"weight": 2.0, "description": "CI on every PR catches defects pre-merge"},
    "canary_deployment":  {"weight": 2.0, "description": "Progressive rollout catches prod-only failures"},
    "automated_rollback": {"weight": 1.5, "description": "Instant revert on anomaly detection"},
    "observability":      {"weight": 1.5, "description": "Alerting + metrics detect failures fast"},
    "wave_deployment":    {"weight": 1.0, "description": "Stage → preprod → prod progression"},
    "feature_flags":      {"weight": 1.0, "description": "Decouple deploy from release"},
    "blue_green":         {"weight": 0.5, "description": "Zero-downtime deploy infrastructure"},
}

_MATURITY_MAX_SCORE = sum(d["weight"] for d in MATURITY_DIMENSIONS.values())  # 9.5

_MATURITY_TIERS = [
    (4.0,  "foundational"),
    (7.0,  "intermediate"),
    (float("inf"), "advanced"),
]

# ---------------------------------------------------------------------------
# Preset deployment profiles
# ---------------------------------------------------------------------------

_PRESET_PROFILES: dict[str, dict[str, Any]] = {
    "cd": {
        "release_cadence": "cd",
        "prs_per_release": 1,
        "releases_stacked": 1,
        "maturity": {
            "automated_testing": 1.0,
            "canary_deployment": 1.0,
            "automated_rollback": 1.0,
            "observability": 1.0,
            "wave_deployment": 1.0,
            "feature_flags": 0.8,
            "blue_green": 0.8,
        },
    },
    "daily-train": {
        "release_cadence": "daily-train",
        "prs_per_release": 0,  # 0 = compute from queue_size / 5
        "releases_stacked": 2,
        "maturity": {
            "automated_testing": 0.8,
            "canary_deployment": 0.5,
            "automated_rollback": 0.3,
            "observability": 0.6,
            "wave_deployment": 0.3,
            "feature_flags": 0.4,
            "blue_green": 0.2,
        },
    },
    "weekly-train": {
        "release_cadence": "weekly-train",
        "prs_per_release": 0,  # 0 = compute from queue_size
        "releases_stacked": 3,
        "maturity": {
            "automated_testing": 0.6,
            "canary_deployment": 0.2,
            "automated_rollback": 0.1,
            "observability": 0.4,
            "wave_deployment": 0.1,
            "feature_flags": 0.2,
            "blue_green": 0.0,
        },
    },
    "manual": {
        "release_cadence": "manual",
        "prs_per_release": 0,  # 0 = compute from queue_size
        "releases_stacked": 5,
        "maturity": {
            "automated_testing": 0.3,
            "canary_deployment": 0.0,
            "automated_rollback": 0.0,
            "observability": 0.2,
            "wave_deployment": 0.0,
            "feature_flags": 0.0,
            "blue_green": 0.0,
        },
    },
}


# ---------------------------------------------------------------------------
# Core formulas
# ---------------------------------------------------------------------------

def merge_batch_success(defect_rate: float, batch_size: float) -> float:
    """Probability that a merge batch of *batch_size* commits has zero defects.

    Joe Magerramov's formula: (1 - defect_rate) ^ batch_size
    Returns 0.0–1.0.
    """
    if batch_size <= 0 or defect_rate <= 0:
        return 1.0
    if defect_rate >= 1.0:
        return 0.0
    return (1.0 - defect_rate) ** batch_size


def release_train_success(defect_rate: float, prs_per_release: int) -> float:
    """Probability that a release train of *prs_per_release* PRs ships clean.

    Same formula as merge_batch_success but applied at the release level.
    CD is the special case where prs_per_release == 1.
    When prs_per_release > merge batch size, risk compounds because the
    entire train fails if any single PR is defective.
    """
    return merge_batch_success(defect_rate, prs_per_release)


def rollback_feasibility(releases_stacked: int, avg_prs_per_release: int = 10) -> dict:
    """Assess whether rollback is feasible given stacked releases.

    When a bug in release R1 is found after R2, R3, R4 are already deployed,
    rolling back to pre-R1 requires reverting all subsequent releases too.
    At >= 4 stacked releases, rollback is effectively impossible and
    roll-forward (fix-forward) is the only option.

    Returns dict with: can_rollback, strategy, mttr_multiplier, reason,
    total_prs_at_risk.
    """
    total_prs = releases_stacked * avg_prs_per_release

    if releases_stacked <= 1:
        return {
            "can_rollback": True,
            "strategy": "rollback",
            "mttr_multiplier": 1.0,
            "reason": "Single release — clean rollback is straightforward.",
            "total_prs_at_risk": total_prs,
        }
    if releases_stacked <= 3:
        return {
            "can_rollback": True,
            "strategy": "rollback",
            "mttr_multiplier": 1.5,
            "reason": (
                f"Rollback requires reverting {releases_stacked - 1} intermediate "
                f"release(s) ({total_prs} PRs total). Costly but feasible."
            ),
            "total_prs_at_risk": total_prs,
        }
    return {
        "can_rollback": False,
        "strategy": "roll-forward",
        "mttr_multiplier": 2.5,
        "reason": (
            f"{releases_stacked} stacked releases ({total_prs} PRs). "
            "Rollback is impractical — too many intermediate releases to revert. "
            "Roll-forward (fix-forward) is the only viable strategy."
        ),
        "total_prs_at_risk": total_prs,
    }


def deployment_maturity_score(capabilities: dict[str, float]) -> dict:
    """Score deployment maturity across 7 weighted dimensions.

    Each capability is 0.0 (absent) to 1.0 (fully implemented).
    Unknown keys are ignored; missing keys default to 0.0.

    Returns dict with: maturity_score, maturity_tier,
    effective_risk_multiplier, dimensions (per-dimension breakdown).
    """
    dims: dict[str, dict[str, Any]] = {}
    weighted_sum = 0.0

    for dim_name, dim_info in MATURITY_DIMENSIONS.items():
        value = max(0.0, min(1.0, float(capabilities.get(dim_name, 0.0))))
        weight = dim_info["weight"]
        contribution = value * weight
        weighted_sum += contribution
        dims[dim_name] = {
            "value": value,
            "weight": weight,
            "contribution": round(contribution, 2),
        }

    score = round(weighted_sum, 1)

    tier = "advanced"
    for threshold, tier_name in _MATURITY_TIERS:
        if score < threshold:
            tier = tier_name
            break

    multiplier = round(max(0.3, 1.0 - score / (_MATURITY_MAX_SCORE * 1.43)), 2)

    return {
        "maturity_score": score,
        "maturity_max": _MATURITY_MAX_SCORE,
        "maturity_tier": tier,
        "effective_risk_multiplier": multiplier,
        "dimensions": dims,
    }


def calamity_threshold(
    defect_rate: float,
    batch_size: float,
    target_success: float = 0.70,
) -> dict:
    """Compute the "red line" — the batch size where success drops below target.

    max_safe_batch = log(target_success) / log(1 - defect_rate)

    Returns dict with: current_success, max_safe_batch, headroom_pct, status.
    """
    current = merge_batch_success(defect_rate, batch_size)

    if defect_rate <= 0 or defect_rate >= 1.0:
        max_safe = float("inf") if defect_rate <= 0 else 0.0
    else:
        max_safe = math.log(target_success) / math.log(1.0 - defect_rate)

    if max_safe == float("inf") or max_safe <= 0:
        headroom = 100.0
    elif batch_size <= 0:
        headroom = 100.0
    else:
        headroom = round(max(0.0, (max_safe - batch_size) / max_safe * 100), 1)

    if current >= 0.90:
        status = "green"
    elif current >= target_success:
        status = "yellow"
    else:
        status = "red"

    return {
        "current_success": round(current, 4),
        "max_safe_batch": round(max_safe, 1) if max_safe != float("inf") else None,
        "headroom_pct": headroom,
        "status": status,
        "target_success": target_success,
    }


def deployment_risk_summary(
    defect_rate: float,
    merge_batch_size: float,
    prs_per_release: int,
    maturity_capabilities: dict[str, float],
    releases_stacked: int = 1,
) -> dict:
    """Combine all models into a single deployment risk summary.

    Returns dict with: merge_success, release_success, adjusted_release_success,
    rollback, maturity, calamity, gauge.
    """
    m_success = merge_batch_success(defect_rate, merge_batch_size)
    r_success = release_train_success(defect_rate, prs_per_release)

    maturity = deployment_maturity_score(maturity_capabilities)
    adjusted = min(1.0, r_success + (1.0 - r_success) * (1.0 - maturity["effective_risk_multiplier"]))

    rb = rollback_feasibility(releases_stacked, prs_per_release)
    cal = calamity_threshold(defect_rate, prs_per_release)

    gauge = _build_gauge(adjusted, cal)

    return {
        "merge_batch_success": round(m_success, 4),
        "release_train_success": round(r_success, 4),
        "adjusted_release_success": round(adjusted, 4),
        "rollback": rb,
        "maturity": maturity,
        "calamity": cal,
        "gauge": gauge,
    }


def _build_gauge(current_success: float, calamity: dict, width: int = 15) -> dict:
    """Build a text-based position gauge for the report.

    Returns dict with: bar (str), current_pct, calamity_pct, description.
    """
    current_pct = round(current_success * 100, 1)
    target = calamity["target_success"]
    calamity_pct = round(target * 100, 1)

    current_pos = max(0, min(width - 1, round(current_success * (width - 1))))
    calamity_pos = max(0, min(width - 1, round(target * (width - 1))))

    cells = []
    for i in range(width):
        frac = i / (width - 1) if width > 1 else 0
        if i == current_pos:
            cells.append("🔵")
        elif i == calamity_pos and i != current_pos:
            cells.append("🔴")
        elif frac <= current_success:
            cells.append("🟢")
        elif frac <= target:
            cells.append("🟡")
        else:
            cells.append("░")

    bar = f"[{''.join(cells)}]"
    max_safe = calamity.get("max_safe_batch")
    max_safe_str = f"{max_safe:.0f} PRs/batch" if max_safe is not None else "∞"

    return {
        "bar": bar,
        "current_pct": current_pct,
        "calamity_pct": calamity_pct,
        "description": f"{current_pct}% success | calamity at {max_safe_str}",
    }


# ---------------------------------------------------------------------------
# Deployment profile loader
# ---------------------------------------------------------------------------

def load_deployment_profile(
    profile_name_or_path: str,
    queue_size: int = 0,
) -> dict | None:
    """Load a deployment profile from a preset name or JSON file path.

    Preset names: "cd", "daily-train", "weekly-train", "manual".
    File paths: must exist and contain valid JSON matching the profile schema.

    When prs_per_release is 0 in a preset, it is computed from queue_size:
      - daily-train: queue_size / 5 (workdays)
      - weekly-train / manual: queue_size

    Returns None when profile_name_or_path is empty.
    """
    if not profile_name_or_path:
        return None

    name = profile_name_or_path.strip()
    if not name:
        return None

    if name in _PRESET_PROFILES:
        profile = _deep_copy_profile(_PRESET_PROFILES[name])
        if profile["prs_per_release"] == 0 and queue_size > 0:
            if name == "daily-train":
                profile["prs_per_release"] = max(1, queue_size // 5)
            else:
                profile["prs_per_release"] = max(1, queue_size)
        elif profile["prs_per_release"] == 0:
            profile["prs_per_release"] = 1
        return profile

    if not name.endswith(".json"):
        return None

    path = Path(name)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    return {
        "release_cadence": data.get("release_cadence", "custom"),
        "prs_per_release": int(data.get("prs_per_release", 1)),
        "releases_stacked": int(data.get("releases_stacked", 1)),
        "maturity": {
            k: float(data.get("maturity", {}).get(k, 0.0))
            for k in MATURITY_DIMENSIONS
        },
    }


def _deep_copy_profile(profile: dict) -> dict:
    """Shallow-copy a preset profile so mutations don't affect the preset."""
    return {
        **profile,
        "maturity": dict(profile["maturity"]),
    }
