"""A `gh` executable on PATH that answers from canned files and logs every call.

``FakeGh(tmp_path, monkeypatch)`` installs it. Register answers with ``on(match, stdout, rc=0, stderr="")``: the first
registration whose ``match`` string occurs in the joined argv wins. ``calls`` returns the logged argv lists.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

SCRIPT = r'''#!/usr/bin/env python3
import json, sys, pathlib
base = pathlib.Path(__file__).parent
args = sys.argv[1:]
with open(base / "calls.jsonl", "a") as f:
    f.write(json.dumps(args) + "\n")
joined = " ".join(args)
rules = json.loads((base / "rules.json").read_text()) if (base / "rules.json").exists() else []
for r in rules:
    if r["match"] in joined:
        if r.get("once"):
            r["match"] = "\0never"
            (base / "rules.json").write_text(json.dumps(rules))
        sys.stdout.write(r["stdout"]); sys.stderr.write(r.get("stderr", "")); sys.exit(r.get("rc", 0))
sys.stderr.write("fake gh: no rule for: " + joined + "\n"); sys.exit(1)
'''


class FakeGh:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.dir = tmp_path / "fake-gh"
        self.dir.mkdir()
        exe = self.dir / "gh"
        exe.write_text(SCRIPT)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{self.dir}:/usr/bin:/bin")
        self.rules: list[dict] = []

    def on(self, match: str, stdout: str | dict | list = "", rc: int = 0, stderr: str = "", once: bool = False) -> "FakeGh":
        if not isinstance(stdout, str):
            stdout = json.dumps(stdout)
        self.rules.append({"match": match, "stdout": stdout, "rc": rc, "stderr": stderr, "once": once})
        (self.dir / "rules.json").write_text(json.dumps(self.rules))
        return self

    @property
    def calls(self) -> list[list[str]]:
        path = self.dir / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def calls_matching(self, needle: str) -> list[list[str]]:
        return [c for c in self.calls if needle in " ".join(c)]
