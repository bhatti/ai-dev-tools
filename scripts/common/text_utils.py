"""Text processing helpers shared across pipeline modules."""

import re

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def slug(title: str) -> str:
    """Convert an issue title to a URL-safe lowercase slug."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]


def strip_ansi(text: str) -> str:
    """Remove terminal colour/cursor escape sequences (browser and test-runner errors carry them)."""
    return _ANSI.sub("", text)


_CODE_TABLE = str.maketrans("0123456789", "abcdefghij")


def normalize_log_line(line: str) -> str:
    """Strip volatile tokens so the same error clusters across runs."""
    s = line.strip().lower()
    s = re.sub(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?z?", "<ts>", s)
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<uuid>", s)
    s = re.sub(r"\b[0-9a-hjkmnp-tv-z]{26}\b", "<ulid>", s)
    s = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", "<ip>", s)
    s = re.sub(r"\b[0-9a-f]{12,}\b", "<hex>", s)
    # Keep HTTP-ish status codes distinguishable (status 404 vs 500), then drop every other number.
    s = re.sub(r"(?i)\b(status|code|http)([\s:=]*)([1-5]\d\d)\b", lambda m: f"{m.group(1)}{m.group(2)}" + m.group(3).translate(_CODE_TABLE), s)
    s = re.sub(r"\d+", "N", s)  # also catches digits glued to units, e.g. 31ms
    return s[:200]
