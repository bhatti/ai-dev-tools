"""Tests for scripts/slack/deploy_workflows.py"""
import contextlib
from unittest.mock import patch, MagicMock, mock_open

import pytest


def _run_main(*args):
    """Run deploy_workflows.main() with argv patched to args."""
    import sys
    from scripts.slack.deploy_workflows import main
    with patch.object(sys, "argv", ["deploy_workflows"] + list(args)):
        main()


@contextlib.contextmanager
def _fake_token(token="fake-token"):
    """Suppress ~/.zshrc read and override env so the script sees a controlled token.

    deploy_workflows.main() prefers ~/.zshrc over os.environ; we suppress the file
    existence check so it falls through to the env var we inject.
    """
    import os as _os
    _real_exists = _os.path.exists
    def _no_zshrc(p):
        if str(p).endswith(".zshrc"):
            return False
        return _real_exists(p)
    with patch("os.path.exists", side_effect=_no_zshrc), \
         patch.dict("os.environ", {"FORMICARY_TOKEN": token}):
        yield


# ---------------------------------------------------------------------------
# --set-config KEY VALUE
# ---------------------------------------------------------------------------

def test_set_config_dry_run_prints_without_pushing(capsys):
    """`--set-config KEY VALUE --dry-run` prints but never calls _push_org_config."""
    with _fake_token(), \
         patch("scripts.slack.deploy_workflows._resolve_org_id", return_value="org-abc"), \
         patch("scripts.slack.deploy_workflows._push_org_config") as mock_push:
        _run_main("--set-config", "BASE_BRANCH", "stage", "--dry-run",
                  "--server", "http://localhost:7777")

    mock_push.assert_not_called()
    out = capsys.readouterr().out
    assert "BASE_BRANCH" in out
    assert "stage" in out


def test_set_config_calls_push_org_config(capsys):
    """`--set-config` without --dry-run calls _push_org_config with the key/value."""
    with _fake_token(), \
         patch("scripts.slack.deploy_workflows._resolve_org_id", return_value="org-abc"), \
         patch("scripts.slack.deploy_workflows._push_org_config", return_value=True) as mock_push:
        _run_main("--set-config", "BASE_BRANCH", "stage",
                  "--server", "http://localhost:7777")

    mock_push.assert_called_once_with(
        "http://localhost:7777", "fake-token", "org-abc", "BASE_BRANCH", "stage"
    )


def test_set_config_multiple_pairs():
    """`--set-config` can be repeated to set multiple keys in one invocation."""
    calls = []

    def fake_push(base_url, token, org_id, name, value):
        calls.append((name, value))
        return True

    with _fake_token(), \
         patch("scripts.slack.deploy_workflows._resolve_org_id", return_value="org-abc"), \
         patch("scripts.slack.deploy_workflows._push_org_config", side_effect=fake_push):
        _run_main(
            "--set-config", "BASE_BRANCH", "stage",
            "--set-config", "UserTag", "alice",
            "--server", "http://localhost:7777",
        )

    assert ("BASE_BRANCH", "stage") in calls
    assert ("UserTag", "alice") in calls


def test_set_config_exits_nonzero_on_push_failure():
    """`--set-config` exits 1 if _push_org_config returns False."""
    with _fake_token(), \
         patch("scripts.slack.deploy_workflows._resolve_org_id", return_value="org-abc"), \
         patch("scripts.slack.deploy_workflows._push_org_config", return_value=False):
        with pytest.raises(SystemExit) as exc:
            _run_main("--set-config", "KEY", "val",
                      "--server", "http://localhost:7777")
    assert exc.value.code == 1
