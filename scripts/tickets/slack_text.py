"""Making untrusted ticket text safe for one line of a Slack message: no mentions, no link markup, no secrets, no overlong lines."""

from __future__ import annotations

import re

from scripts.common.redact import redact

SAFE_URL = re.compile(r"^https?://[^\s<>|`]+$")
IDS_SHOWN = 12


def escape(text: object) -> str:
    """Slack control characters made inert (no mentions, channel pings or link markup)."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ").strip()


def clip(text: object, limit: int) -> str:
    """Secrets removed first (so a limit cannot leave a fragment of one), cut at a word boundary, and only then escaped (so an entity is never cut in half)."""
    flat = redact(" ".join(str(text).split()))
    if len(flat) > limit:
        cut = flat[:limit]
        flat = (cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else cut).rstrip() + "…"
    return escape(flat)


def ids(values: list[str]) -> str:
    """Ticket ids, escaped, at most ``IDS_SHOWN`` of them and a count of the rest (never cut in the middle of an id)."""
    shown = ", ".join(escape(i) for i in values[:IDS_SHOWN])
    return shown + (f" (+{len(values) - IDS_SHOWN} more)" if len(values) > IDS_SHOWN else "")
