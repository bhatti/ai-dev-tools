"""Redact credentials from text and JSON before it goes into reports, logs, artifacts or tracker comments.

Every pattern is linear-time on hostile input: keyword matches are bounded (``{0,40}``) and nothing nests an
unbounded quantifier, because the inputs are CI logs and PR text an attacker can shape.
"""

from __future__ import annotations

import re
from typing import Any

_KEYWORDS = r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|credential|session[_-]?id|auth(?:orization)?)"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]{0,30}PRIVATE KEY-----|\Z)"), "<redacted-private-key>"),
    (re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key|cookie|set-cookie)[ \t]*:[ \t]*[^\r\n]+"), r"\1: <redacted>"),
    (re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <redacted>"),
    # key = value / "key": "value" / KEY='value' / stringified JSON ({\"password\":\"x\"}). Matching starts at the keyword (a prefix such
    # as AWS_SECRET_ACCESS_KEY is irrelevant for finding the value), which keeps the scan linear.
    (re.compile(r"(?i)(" + _KEYWORDS + r"""[\w-]{0,40}(?:\\?["'])?[ \t]*[:=][ \t]*)(\\?"(?:[^"\\\r\n]|\\.){0,2000}\\?"|'[^'\r\n]{0,2000}'|[^\s,;&"'}\]]+)"""), r"\1<redacted>"),
    # command-line flags: --password hunter2, --token=abc
    (re.compile(r"(?i)(--(?:password|passwd|token|secret|api-?key|access-?key|client-?secret)(?:[ \t]+|=))(\S+)"), r"\1<redacted>"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"), "<redacted-github-token>"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "<redacted-slack-token>"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,20}://[^\s:/@]{1,100}):[^\s@/]{1,200}@"), r"\1:<redacted>@"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<redacted-jwt>"),
]
_SECRET_KEY = re.compile(r"(?i)" + _KEYWORDS)


def redact(text: str) -> str:
    """Return ``text`` with secrets replaced by placeholders. Safe to call on any string."""
    text = text or ""
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return text


def redact_obj(obj: Any) -> Any:
    """Recursively redact a JSON-like structure: every string value, and any value whose key names a secret."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, dict):
        return {k: ("<redacted>" if isinstance(v, str) and _SECRET_KEY.search(str(k)) else redact_obj(v)) for k, v in obj.items()}
    return obj
