"""Print review signals for PRs: ``python -m scripts.common.pr_signals --tracker github 12 13``.

Reads the repository from the same environment as the workflows (GH_ORG/GH_REPO or BITBUCKET_*). Handy for checking
what a report will show and how many requests it costs.
"""

from __future__ import annotations

import argparse
import json
import sys

from scripts.common.config import load_config
from scripts.common.pr_signals import GROUPS, collect_signals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("numbers", nargs="+", type=int)
    parser.add_argument("--tracker", choices=("github", "bitbucket"), required=True)
    parser.add_argument("--needs", default=",".join(GROUPS), help="comma-separated groups: " + ", ".join(GROUPS))
    args = parser.parse_args(argv)
    needs = tuple(n for n in args.needs.split(",") if n)
    unknown = set(needs) - set(GROUPS)
    if unknown:
        print(f"ERROR: unknown group(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    records = collect_signals([{"number": n} for n in args.numbers], args.tracker, load_config(required=[]), needs)
    print(json.dumps(records, indent=2, sort_keys=True))
    return 1 if any(r.get("error") for r in records.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
