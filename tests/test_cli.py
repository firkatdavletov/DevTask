"""CLI entry-point compatibility and centralized failure handling."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from dev_task.cli import main
from dev_task.errors import TaskError


SCRIPT = Path(__file__).resolve().parents[1] / "dev-task"
ARGS = ["run", "card.md", "--config", "config.json"]


class CliTest(unittest.TestCase):
    def invoke(self, *args, direct=False, no_site=False):
        with tempfile.TemporaryDirectory() as directory:
            argv = [str(SCRIPT)] if direct else [sys.executable, *(["-S"] if no_site else []), str(SCRIPT)]
            env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
            # Resolve the package solely through the script's directory.
            env.pop("PYTHONPATH", None)
            return subprocess.run([*argv, *args], cwd=directory, env=env, text=True,
                                  capture_output=True, timeout=10)

    def test_help_from_other_directory_for_both_launch_methods(self):
        for direct in (False, True):
            with self.subTest(direct=direct):
                result = self.invoke("--help", direct=direct)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("{run,publish,report,review}", result.stdout)
                self.assertIn("--continue-pr", result.stdout)
                self.assertIn("--skip-planning", result.stdout)
                self.assertEqual(result.stderr, "")

    def test_argument_errors_keep_argparse_exit_code(self):
        result = self.invoke("unknown")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_missing_yaml_keeps_dependency_message(self):
        result = self.invoke("--help", no_site=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Установите зависимости:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_flag_combinations_stop_before_workflow(self):
        for argv in (["publish", "card.md", "--config", "config.json", "--local-only"],
                     [*ARGS, "--continue-pr", "--rerun"]):
            with self.subTest(argv=argv), patch("dev_task.cli.run_workflow") as workflow:
                with contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(main(argv), 1)
                self.assertIn("dev-task:", stderr.getvalue())
                workflow.assert_not_called()

    def test_main_accepts_explicit_arguments(self):
        with patch("dev_task.cli.run_workflow") as workflow:
            self.assertEqual(main([*ARGS, "--local-only"]), 0)
        args = workflow.call_args.args[0]
        self.assertEqual(args.command, "run")
        self.assertEqual(args.card, Path("card.md"))
        self.assertTrue(args.local_only)

    def test_expected_errors_have_message_and_exit_code_one(self):
        for error in (TaskError("failure"), OSError("failure"), ValueError("failure"),
                      yaml.YAMLError("failure"), subprocess.SubprocessError("failure")):
            with self.subTest(error=type(error)), patch("dev_task.cli.run_workflow", side_effect=error):
                with contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(main(ARGS), 1)
                self.assertEqual(stderr.getvalue(), "dev-task: failure\n")

    def test_interrupt_keeps_exit_code_and_message(self):
        with patch("dev_task.cli.run_workflow", side_effect=KeyboardInterrupt):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(ARGS), 130)
        self.assertIn("Worktree и состояние сохранены", stdout.getvalue())
