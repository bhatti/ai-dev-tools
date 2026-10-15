"""Extract the failing test's file and name from free text (issue body, CI log, stack trace).

Handles the common runner output formats; the text is untrusted, so results are only *candidates*:
callers must confirm the file exists in the checkout before running anything.

Usage:
    python3 -m scripts.flaky.identify --text-file ctx.txt [--repo DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# The input is untrusted CI text: every pattern starts at a path boundary (the lookbehind) so a long run of word
# characters is scanned once, not once per start position.
_B = r"(?<![\w./@:-])"  # also not after "scheme:" so URLs yield nothing
MAX_TEXT = 500_000
_PATTERNS: list[tuple[str, re.Pattern[str], tuple[int, int]]] = [
    # (kind, regex, (file_group, name_group)); 0 = absent
    ("pytest", re.compile(_B + r"([\w./-]{1,300}\.py)::((?:[\w:.-]|\[[^\]\n]{0,200}\]){1,300})"), (1, 2)),
    ("jest", re.compile(r"^\s*FAIL\s+([\w./@-]{1,300}\.(?:test|spec)\.[cm]?[jt]sx?)", re.M), (1, 0)),
    ("jest-case", re.compile(r"●\s+(.{1,200}?)\s+›\s+(.{1,300})$", re.M), (0, 2)),
    ("js-stack", re.compile(_B + r"([\w./@-]{1,300}\.(?:test|spec)\.[cm]?[jt]sx?):\d+"), (1, 0)),
    ("go", re.compile(r"--- FAIL: (Test\w{1,200})"), (0, 1)),
    ("rspec", re.compile(r"rspec (\./?spec/[\w/.-]{1,300}_spec\.rb)(?::\d+)?"), (1, 0)),
    ("junit", re.compile(r"\b([A-Za-z_]\w{0,100}(?:\.[A-Za-z_]\w{0,100}){0,10})\.(test\w{1,100}|\w{1,100}Test)\s+(?:FAILED|<<< FAILURE)"), (1, 2)),
    ("explicit", re.compile(r"""(?:file|path)\s*[=:]\s*['"`]?([\w./@-]{1,300}\.\w{1,5})['"`]?"""), (1, 0)),
]
_TEST_FILE_HINT = re.compile(r"(test|spec|_test)", re.I)


def extract_test_refs(text: str, repo_dir: str | Path | None = None, limit: int = 10) -> list[dict]:
    """Return ranked candidates ``[{file, name, kind, count, exists}]`` (most frequently mentioned first)."""
    counts: Counter[tuple[str, str, str]] = Counter()
    for kind, rx, (fg, ng) in _PATTERNS:
        for m in rx.finditer((text or "")[:MAX_TEXT]):
            file = m.group(fg).strip() if fg else ""
            name = m.group(ng).strip() if ng else ""
            if file and not _TEST_FILE_HINT.search(file) and kind in ("explicit", "js-stack"):
                continue
            if file.startswith(("http://", "https://", "node_modules/")):
                continue
            counts[(file.removeprefix("./"), name, kind)] += 1
    # A name-only hit (go, jest-case) is attached to the most common file of the same text if exactly one exists.
    files = {f for f, _, _ in counts if f}
    out: list[dict] = []
    for (file, name, kind), n in counts.most_common():
        exists = None
        if repo_dir is not None and file:
            root = Path(repo_dir).resolve()
            candidate = (root / file).resolve()
            exists = candidate.is_relative_to(root) and candidate.exists()  # a path from CI text must not probe outside the checkout
        out.append({"file": file, "name": name, "kind": kind, "count": n, "exists": exists})
    if len(files) == 1:
        only = next(iter(files))
        for item in out:
            if not item["file"]:
                item["file"] = only
    # Candidates whose file is known to exist come first, then by count.
    out.sort(key=lambda r: (r["exists"] is False, -r["count"]))
    return out[:limit]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--text-file", required=True)
    p.add_argument("--repo", default=None)
    args = p.parse_args(argv)
    text = Path(args.text_file).read_text(encoding="utf-8", errors="replace")
    refs = extract_test_refs(text, args.repo)
    print(json.dumps({"candidates": refs}, indent=2))
    return 0 if refs else 2


if __name__ == "__main__":
    sys.exit(main())
