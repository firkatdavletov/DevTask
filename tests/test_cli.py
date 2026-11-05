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
from dev_task.events import ProgressEvent
from dev_task.models import OperationResult


SCRIPT = Path(__file__).resolve().parents[1] / "dev-task"
ARGS = ["run", "card.md", "--config", "config.json"]


class CliTest(unittest.TestCase):
    def setUp(self):
        project = patch("dev_task.cli.load_project")
        project.start()
        self.addCleanup(project.stop)

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
            with self.subTest(argv=argv), patch("dev_task.cli.Orchestrator.run") as workflow:
                with contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(main(argv), 1)
                self.assertIn("dev-task:", stderr.getvalue())
                workflow.assert_not_called()

    def test_main_accepts_explicit_arguments(self):
        with tempfile.TemporaryDirectory() as directory, patch("dev_task.cli.Orchestrator.run") as workflow:
            run_dir = Path(directory)
            (run_dir / "report.md").write_text("", encoding="utf-8")
            workflow.return_value = OperationResult("completed", run_dir)
            self.assertEqual(main([*ARGS, "--local-only"]), 0)
        args = workflow.call_args.args[0]
        self.assertEqual(args.card, Path("card.md"))
        self.assertTrue(args.local_only)

    def run_with_report(self, command, run_dir, *, error=None, updates=(), result_dir=None):
        with patch("dev_task.cli.Orchestrator") as orchestrator:
            def execute(request):
                callback = orchestrator.call_args.kwargs["on_event"]
                for path, content in updates:
                    path.write_text(content, encoding="utf-8", newline="")
                    callback(ProgressEvent("report", status="updated", details={"report_path": str(path)}))
                callback(ProgressEvent("progress", message="Завершение операции"))
                if error is not None:
                    raise error
                return OperationResult("completed", result_dir or run_dir)

            getattr(orchestrator.return_value, command).side_effect = execute
            argv = [command, "card.md"] + (["--base", "main"] if command == "review" else [])
            with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
                code = main(argv)
            output = stdout.getvalue()
            self.assertIn("[Начато] Загрузка проекта\n", output)
            output = "\n".join(line for line in output.split("\n") if not line.startswith(("[Начато] Загрузка проекта", "[Готово] Загрузка проекта")))
            return code, output, stderr.getvalue()

    def test_all_commands_print_result_report_after_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            content = "# Отчёт\r\n\nUnicode: ✓\nБез завершающего перевода"
            (run_dir / "report.md").write_text(content, encoding="utf-8", newline="")
            for command in ("run", "publish", "report", "review"):
                with self.subTest(command=command):
                    code, stdout, stderr = self.run_with_report(command, run_dir)
                    self.assertEqual(code, 0)
                    self.assertTrue(stdout.endswith("Завершение операции\n" + content))
                    self.assertEqual(stderr, "")

    def test_only_latest_report_is_printed_on_success_error_and_interrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            first = run_dir / "first.md"
            latest = run_dir / "report.md"
            for error, expected_code in ((None, 0), (TaskError("failure"), 1), (KeyboardInterrupt(), 130)):
                with self.subTest(error=type(error)):
                    code, stdout, stderr = self.run_with_report(
                        "run", run_dir, error=error,
                        updates=((first, "intermediate"), (latest, "old"), (latest, "# Итог\n")))
                    self.assertEqual(code, expected_code)
                    self.assertNotIn("intermediate", stdout)
                    self.assertNotIn("old", stdout)
                    self.assertEqual(stdout.count("# Итог\n"), 1)
                    self.assertTrue(stdout.endswith("# Итог\n"))
                    self.assertEqual(stderr, "dev-task: failure\n" if expected_code == 1 else "")

    def test_success_uses_result_path_instead_of_last_event(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            result_dir = run_dir / "result"
            result_dir.mkdir()
            (result_dir / "report.md").write_text("final", encoding="utf-8")
            code, stdout, stderr = self.run_with_report(
                "run", run_dir, result_dir=result_dir, updates=((run_dir / "report.md", "intermediate"),))
            self.assertEqual((code, stdout, stderr), (0, "Завершение операции\nfinal", ""))

    def test_report_path_does_not_leak_between_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            self.run_with_report("run", run_dir, updates=((run_dir / "report.md", "old report"),))
            code, stdout, stderr = self.run_with_report("run", run_dir, error=TaskError("early failure"))
            self.assertEqual((code, stdout, stderr),
                             (1, "Завершение операции\n", "dev-task: early failure\n"))

    def test_report_read_failures_preserve_operation_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            for read_error in (FileNotFoundError("missing"), PermissionError("denied"),
                               UnicodeError("invalid UTF-8")):
                for error, expected_code in ((None, 0), (TaskError("failure"), 1), (KeyboardInterrupt(), 130)):
                    with self.subTest(read_error=type(read_error), error=type(error)):
                        with patch.object(Path, "open", side_effect=read_error):
                            # write_text is mocked separately so an update can identify the report before failure.
                            with patch.object(Path, "write_text"):
                                code, stdout, stderr = self.run_with_report(
                                    "run", run_dir, error=error, updates=((run_dir / "report.md", "report"),))
                        self.assertEqual(code, expected_code)
                        self.assertIn("Не удалось прочитать отчёт", stderr)
                        self.assertNotIn("report\n", stdout)

    def test_expected_errors_have_message_and_exit_code_one(self):
        for error in (TaskError("failure"), OSError("failure"), ValueError("failure"),
                      yaml.YAMLError("failure"), subprocess.SubprocessError("failure")):
            with self.subTest(error=type(error)), patch("dev_task.cli.Orchestrator.run", side_effect=error):
                with contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(main(ARGS), 1)
                self.assertEqual(stderr.getvalue(), "dev-task: failure\n")

    def test_interrupt_keeps_exit_code_and_message(self):
        with patch("dev_task.cli.Orchestrator.run", side_effect=KeyboardInterrupt):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(ARGS), 130)
        self.assertIn("Worktree и состояние сохранены", stdout.getvalue())
