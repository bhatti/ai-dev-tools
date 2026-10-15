"""Environment for running code we did not write (a PR's tests, build and start commands).

The platform's own credentials (tracker, LLM, chat, SSH, cloud) are loaded into the job environment so the
tooling can talk to GitHub/Bitbucket/Jira. Test and build code from a PR branch must not inherit them: a
postinstall script or a test could read and exfiltrate them.

Usage (install steps, service starts and anything else that executes repository code):
    python3 -m scripts.common.env_scrub -- npm ci
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

PLATFORM_SECRETS = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "BITBUCKET_TOKEN", "BITBUCKET_APP_PASSWORD", "GH_TOKEN", "GITHUB_TOKEN",
    "JIRA_API_TOKEN", "JIRA_AUTH", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_SIGNING_SECRET", "SSH_PRIVATE_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "JobAPIToken", "JOB_API_TOKEN", "FORMICARY_TOKEN",
    "SSH_AUTH_SOCK",
})
# Anything that merely looks like a credential is dropped too: a name list can never be complete.
_SECRET_NAME = re.compile(r"(?i)TOKEN|SECRET|PASSW(?:OR)?D|API_?KEY|PRIVATE_?KEY|SSH_?KEY|CREDENTIAL|SESSION_?KEY|(?:^|_)AUTH(?:ORIZATION)?(?:_|$)|WEBHOOK")
_TARGET_CREDENTIALS = frozenset({"QA_USERNAME", "QA_PASSWORD", "QA_AUTH_HEADER"})


def scrubbed_environ(extra: dict[str, str] | None = None, keep_qa: bool = False, keep: tuple[str, ...] = ()) -> dict[str, str]:
    """``os.environ`` without credentials, plus ``extra``.

    Dropped: the platform's own secrets and every variable whose name looks like a credential (token, secret,
    password, api key, private/ssh key, auth, webhook). ``keep`` names variables the code under test genuinely needs
    (e.g. an npm registry token); ``QA_USERNAME`` / ``QA_PASSWORD`` / ``QA_AUTH_HEADER`` (credentials for the target
    under test) are dropped unless ``keep_qa``: only the test command needs them, never the build or start of the code.
    """
    def allowed(name: str) -> bool:
        if name in keep:
            return True
        if name in _TARGET_CREDENTIALS:
            return keep_qa
        return name not in PLATFORM_SECRETS and not _SECRET_NAME.search(name)

    env = {k: v for k, v in os.environ.items() if allowed(k)}
    env.update(extra or {})
    return env


def main(argv: list[str] | None = None) -> int:
    """Run a command with the platform's credentials removed from its environment; exit with its status."""
    p = argparse.ArgumentParser(description=main.__doc__)
    p.add_argument("--keep-qa", action="store_true", help="keep QA_USERNAME/QA_PASSWORD/QA_AUTH_HEADER (for the test command only)")
    p.add_argument("--keep", action="append", default=[], metavar="NAME", help="keep one variable the code genuinely needs, e.g. NPM_TOKEN")
    p.add_argument("command", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not command:
        print("a command is required after --", file=sys.stderr)
        return 2
    try:
        return subprocess.run(command, env=scrubbed_environ(keep_qa=a.keep_qa, keep=tuple(a.keep))).returncode
    except OSError as e:
        print(f"cannot run {command[0]!r}: {e}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
