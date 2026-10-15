"""Detect a repo's test runner and build full-file / single-test commands.

Language-agnostic by design: it recognizes the common runners from their manifest files and returns
argv lists (never shell strings) so test names cannot inject shell syntax. Anything unrecognized
falls back to a caller-supplied ``--cmd``.

Usage:
    python3 -m scripts.flaky.detect_runner --repo DIR --file path/to/test [--name "test name"]
Prints JSON: {"runner": ..., "root": ..., "full": [...], "isolated": [...]|null, "evidence": ...}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _node_framework(pkg_json: str, test_file: str) -> tuple[str, str]:
    try:
        pkg = json.loads(pkg_json)
    except json.JSONDecodeError:
        pkg = {}
    blob = json.dumps({k: pkg.get(k, {}) for k in ("dependencies", "devDependencies", "scripts")})
    in_e2e = bool(re.search(r"(^|/)(e2e|playwright|integration-ui)(/|$)", test_file))
    if "@playwright/test" in blob and (in_e2e or not any(f in blob for f in ("jest", "vitest", "mocha"))):
        return "playwright", "package.json: @playwright/test"
    for name in ("vitest", "jest", "mocha"):
        if name in blob:
            return name, f"package.json: {name}"
    return "", ""


def detect_for_file(repo_dir: str | Path, test_file: str) -> dict | None:
    """Find the runner governing ``test_file`` by walking up to the repo root (nearest manifest wins)."""
    repo = Path(repo_dir).resolve()
    start = (repo / test_file).resolve().parent
    if repo not in [start, *start.parents]:
        start = repo
    for d in [start, *start.parents]:
        found = _detect_in_dir(d, test_file)
        if found:
            return found
        if d == repo:
            break
    return None


def _detect_in_dir(d: Path, test_file: str) -> dict | None:
    def hit(runner: str, evidence: str) -> dict:
        return {"runner": runner, "root": str(d), "evidence": evidence}

    if (d / "package.json").exists():
        fw, ev = _node_framework(_read(d / "package.json"), test_file)
        if fw:
            return hit(fw, ev)
    py_cfg = _read(d / "pyproject.toml") + _read(d / "pytest.ini") + _read(d / "setup.cfg") + _read(d / "tox.ini")
    if (d / "pytest.ini").exists() or "pytest" in py_cfg or (d / "conftest.py").exists():
        return hit("pytest", "pytest config/conftest")
    if (d / "go.mod").exists():
        return hit("gotest", "go.mod")
    if (d / "pom.xml").exists():
        return hit("maven", "pom.xml")
    if (d / "build.gradle").exists() or (d / "build.gradle.kts").exists():
        return hit("gradle", "build.gradle")
    if (d / "Cargo.toml").exists():
        return hit("cargo", "Cargo.toml")
    if (d / "Gemfile").exists() and (d / "spec").is_dir():
        return hit("rspec", "Gemfile + spec/")
    if any(d.glob("*.csproj")) or any(d.glob("*.sln")):
        return hit("dotnet", "csproj/sln")
    if (d / "requirements.txt").exists() and "pytest" in _read(d / "requirements.txt"):
        return hit("pytest", "requirements.txt: pytest")
    return None


def _regex(name: str) -> str:
    """Literal test name as a regex (jest -t, mocha --grep and playwright -g take patterns)."""
    return re.sub(r"([.^$*+?{}()|\[\]\\])", r"\\\1", name)


def build_commands(runner: str, root: str | Path, test_file: str, test_name: str = "") -> dict:
    """Return {'full': argv, 'isolated': argv|None, 'cwd': root} for the given runner.

    ``full`` runs the whole test file/package (catches suite pollution); ``isolated`` runs just the
    named test (reproduces the flake with the least interference).
    """
    root = str(root)
    f, n = test_file, test_name
    for what, value in (("test file", f), ("test name", n)):
        if value.startswith("-"):
            raise ValueError(f"{what} must not start with '-' (it would be read as a runner option): {value!r}")
    full: list[str]
    iso: list[str] | None = None
    if runner in ("jest", "vitest", "mocha", "playwright"):
        pre = {"jest": ["npx", "--no-install", "jest"], "vitest": ["npx", "--no-install", "vitest", "run"],
               "mocha": ["npx", "--no-install", "mocha"], "playwright": ["npx", "--no-install", "playwright", "test"]}[runner]
        flag = {"jest": "-t", "vitest": "-t", "mocha": "--grep", "playwright": "-g"}[runner]
        full = [*pre, f]
        iso = [*pre, f, flag, _regex(n)] if n else None
    elif runner == "pytest":
        base = ["python3", "-m", "pytest", "-p", "no:cacheprovider"]
        full = [*base, f]
        if not n:
            iso = None
        elif "::" in f:
            iso = full
        elif re.fullmatch(r"[\w\[\]\-.]+(::[\w\[\]\-.]+)*", n):
            iso = [*base, f"{f}::{n}"]
        else:
            iso = [*base, f, "-k", n]
    elif runner == "gotest":
        parent = Path(f).parent.as_posix() if f.endswith(".go") else f
        pkg = "." if parent in (".", "") else "./" + parent.removeprefix("./")
        full = ["go", "test", "-count=1", pkg]
        iso = ["go", "test", "-count=1", "-run", f"^{re.escape(n)}$", pkg] if n else None
    elif runner == "maven":
        cls = Path(f).stem
        full = ["mvn", "-q", f"-Dtest={cls}", "test"]
        iso = ["mvn", "-q", f"-Dtest={cls}#{n}", "test"] if n else None
    elif runner == "gradle":
        gw = "./gradlew" if (Path(root) / "gradlew").exists() else "gradle"
        cls = Path(f).stem
        full = [gw, "cleanTest", "test", "--tests", cls]
        iso = [gw, "cleanTest", "test", "--tests", f"{cls}.{n}"] if n else None
    elif runner == "cargo":
        full = ["cargo", "test", "--test", Path(f).stem] if "/tests/" in f or f.startswith("tests/") else ["cargo", "test"]
        iso = ["cargo", "test", n] if n else None  # a short name only matches as a substring: --exact would need the full module path
    elif runner == "rspec":
        full = ["bundle", "exec", "rspec", f]
        iso = ["bundle", "exec", "rspec", f, "-e", n] if n else None
    elif runner == "dotnet":
        full = ["dotnet", "test", "--no-restore"]
        iso = ["dotnet", "test", "--no-restore", "--filter", f"FullyQualifiedName~{n}"] if n else None
    else:
        raise ValueError(f"unsupported runner {runner!r}; pass an explicit --cmd")
    return {"full": full, "isolated": iso, "cwd": root}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", default=".")
    p.add_argument("--file", required=True)
    p.add_argument("--name", default="")
    args = p.parse_args(argv)
    found = detect_for_file(args.repo, args.file)
    if not found:
        print(json.dumps({"runner": None, "error": "no known test runner detected; pass --cmd explicitly"}))
        return 2
    absolute = Path(args.repo).resolve() / args.file
    rel_to_root = str(absolute.relative_to(found["root"])) if absolute.is_relative_to(found["root"]) else args.file
    try:
        print(json.dumps({**found, **build_commands(found["runner"], found["root"], rel_to_root, args.name)}))
    except ValueError as e:
        print(json.dumps({"runner": found["runner"], "error": str(e)}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
