"""Tests for scripts/common/bootstrap.py"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch


class TestEnsureDebugMode:
    def _run(self, env: dict, marker_exists: bool = False, tmp_path: Path | None = None):
        import importlib
        import scripts.common.bootstrap as bootstrap
        importlib.reload(bootstrap)  # reset module-level state

        if tmp_path:
            marker = tmp_path / ".adt_bootstrap_done"
            bootstrap._DONE_MARKER = marker
            if marker_exists:
                marker.touch()

        with patch.dict(os.environ, env, clear=False):
            yield bootstrap

    def test_skips_when_flag_off(self, tmp_path):
        import scripts.common.bootstrap as bootstrap
        import importlib
        importlib.reload(bootstrap)
        bootstrap._DONE_MARKER = tmp_path / ".adt_bootstrap_done"

        with patch.dict(os.environ, {"AI_DEV_TOOLS_DEBUG": "0"}):
            with patch("scripts.common.bootstrap.subprocess.run") as mock_run:
                bootstrap.ensure_debug_mode()
        mock_run.assert_not_called()

    def test_skips_when_marker_exists(self, tmp_path, capsys):
        import scripts.common.bootstrap as bootstrap
        import importlib
        importlib.reload(bootstrap)
        marker = tmp_path / ".adt_bootstrap_done"
        marker.touch()
        bootstrap._DONE_MARKER = marker

        with patch.dict(os.environ, {"AI_DEV_TOOLS_DEBUG": "1"}):
            with patch("scripts.common.bootstrap.subprocess.run") as mock_run:
                bootstrap.ensure_debug_mode()

        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "already bootstrapped" in out

    def test_clone_skipped_on_second_step_after_first_cloned(self, tmp_path, capsys):
        """Simulates two script steps: bootstrap step clones, record.py step skips."""
        import scripts.common.bootstrap as bootstrap
        import importlib
        importlib.reload(bootstrap)

        marker = tmp_path / ".adt_bootstrap_done"
        app_scripts = tmp_path / "app_scripts"
        app_scripts.mkdir()
        clone_src = tmp_path / "clone" / "scripts"
        clone_src.mkdir(parents=True)
        (clone_src / "foo.py").write_text("x=1")

        bootstrap._DONE_MARKER = marker
        bootstrap._APP_SCRIPTS = app_scripts
        bootstrap._TMP_CLONE = tmp_path / "clone_tmp"

        mock_result = MagicMock()
        mock_result.returncode = 0

        exec_calls = []

        def fake_execv(executable, args):
            # Simulate re-exec: marker exists, AI_DEV_TOOLS_DEBUG already set to "0"
            exec_calls.append(args)
            # Don't actually re-exec — just return (test environment)

        with patch.dict(os.environ, {"AI_DEV_TOOLS_DEBUG": "1"}):
            with patch("scripts.common.bootstrap.subprocess.run", return_value=mock_result):
                with patch("scripts.common.bootstrap.shutil.rmtree"):
                    with patch("scripts.common.bootstrap.shutil.copytree"):
                        with patch("scripts.common.bootstrap.os.execv", side_effect=fake_execv):
                            # Step 1: bootstrap explicit call
                            bootstrap.ensure_debug_mode()

        # After step 1: marker must exist
        assert marker.exists(), "marker must be written before os.execv"
        assert len(exec_calls) == 1, "os.execv must be called exactly once"

        # Step 2: record.py calls ensure_debug_mode with AI_DEV_TOOLS_DEBUG=1 still in env
        with patch.dict(os.environ, {"AI_DEV_TOOLS_DEBUG": "1"}):
            with patch("scripts.common.bootstrap.subprocess.run") as mock_run2:
                with patch("scripts.common.bootstrap.os.execv") as mock_exec2:
                    bootstrap.ensure_debug_mode()

        mock_run2.assert_not_called()   # no second clone
        mock_exec2.assert_not_called()  # no second re-exec
        out = capsys.readouterr().out
        assert "already bootstrapped" in out

    def test_clone_failure_does_not_write_marker(self, tmp_path):
        """If the git clone fails, no marker is written (next attempt should retry)."""
        import scripts.common.bootstrap as bootstrap
        import importlib
        importlib.reload(bootstrap)

        marker = tmp_path / ".adt_bootstrap_done"
        bootstrap._DONE_MARKER = marker
        bootstrap._TMP_CLONE = tmp_path / "clone_tmp"

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "connection refused"

        with patch.dict(os.environ, {"AI_DEV_TOOLS_DEBUG": "1"}):
            with patch("scripts.common.bootstrap.subprocess.run", return_value=mock_result):
                with patch("scripts.common.bootstrap.shutil.rmtree"):
                    bootstrap.ensure_debug_mode()

        assert not marker.exists(), "marker must NOT be written when clone fails"
