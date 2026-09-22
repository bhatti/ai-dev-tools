"""Shared utilities for merge-queue scripts.

Centralizes patterns used across scope_router, risk_score, test_impact,
and collect_ready to avoid duplication.
"""
from __future__ import annotations

import re


SENSITIVE_PATHS = re.compile(
    r"(^|/)("
    r"auth|security|billing|payments|crypto|secrets|credentials"
    r"|\.env|migrations|rbac|iam|oauth|tokens"
    r")(/|$|\.)",
    re.IGNORECASE,
)

_TRANSPARENT_PREFIXES = ("src", "lib", "pkg", "internal", "crates", "apps")


def top_level_module(filepath: str) -> str:
    """Extract top-level module/directory from a file path.

    For paths under transparent prefixes (src/, lib/, etc.), returns two
    levels deep. Otherwise returns the first path component.
    """
    parts = filepath.split("/")
    if len(parts) >= 2 and parts[0] in _TRANSPARENT_PREFIXES:
        return parts[0] + "/" + parts[1]
    return parts[0]


def is_test_file(path: str) -> bool:
    """Check if a file path looks like a test file."""
    name = path.split("/")[-1] if "/" in path else path
    return (
        name.startswith("test_")
        or name.endswith("_test.go")
        or name.endswith("_test.py")
        or name.endswith("_test.rs")
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".test.js")
        or name.endswith(".test.jsx")
        or name.endswith("Test.java")
        or name.endswith("Test.kt")
        or name.endswith("_spec.rb")
        or name.endswith("_test.rb")
        or name.endswith("Tests.cs")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.js")
        or "/tests/" in path
        or "/__tests__/" in path
        or "/test/" in path
        or "/spec/" in path
    )
