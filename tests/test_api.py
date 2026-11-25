"""Public API isolation and CLI project selection against a real Git fixture."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_workflow as workflow
from dev_task.api import Orchestrator, load_project
from dev_task.errors import TaskError
from dev_task.events import ExecutionContext, build_environment, emit
from dev_task.models import ReviewRequest, RunRequest, TaskRequest
from dev_task.processes import capture


class ApiTest(unittest.TestCase):
    g = workflow.WorkflowTest.g
    write_config = workflow.WorkflowTest.write_config
    write_card = workflow.WorkflowTest.write_card

    def setUp(self):
        workflow.WorkflowTest.setUp(self)
        self.cfg.pop("repo")
        self.write_config()

    def test_api_runs_silently_and_preserves_environment_and_cwd(self):
        self.write_card(issue="OTHER-7-description")
        self.cfg["env"] = {"DEV_TASK_API_TEST": "configured"}
        self.cfg["setup"] = [{"name": "environment", "argv": [sys.executable, "-c",
            "import os; assert os.environ['DEV_TASK_API_TEST'] == 'configured'"]}]
        self.write_config()
        events = []
        project = load_project(self.repo, self.config)
        cwd = Path.cwd()
        with patch.dict(os.environ, self.env, clear=True):
            before = dict(os.environ)
            with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
                result = Orchestrator(project, on_event=events.append).run(RunRequest(self.card, local_only=True))
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(Path.cwd(), cwd)
        self.assertEqual(result.status, "verified")
        self.assertTrue(result.state_path.is_file())
        self.assertTrue(result.report_path.is_file())
        self.assertEqual(events[0].status, "started")
        self.assertEqual(events[-1].status, "completed")
        steps = [e for e in events if e.kind == "step"]
        names = [e.details["name"] for e in steps if e.status == "started"]
        self.assertLess(names.index("Проверка конфигурации"), names.index("Чтение карточки задачи"))
        self.assertLess(names.index("Получение изменений из Git"), names.index("Создание worktree"))
        self.assertLess(names.index("Создание worktree"), names.index("Разработка"))
        self.assertIn("Сохранение состояния", names)
        active = set()
        for event in steps:
            identifier = event.details["step_id"]
            if event.status == "started":
                if event.details["parent_id"] is not None:
                    self.assertIn(event.details["parent_id"], active)
                active.add(identifier)
            else:
                self.assertIn(identifier, active)
                active.remove(identifier)
        self.assertFalse(active)
        self.assertTrue(any(e.kind == "check" and e.status == "passed" for e in events))
        self.assertTrue(any(e.kind == "agent" and e.stage == "coding" for e in events))
        with patch.dict(os.environ, self.env, clear=True):
            silent = Orchestrator(project)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                silent.report(TaskRequest(self.card))
            self.assertEqual(output.getvalue(), "")

    def test_default_config_and_nested_directory(self):
        config = self.repo / ".dev-task/config.json"
        config.parent.mkdir()
        config.write_text(self.config.read_text())
        nested = self.repo / "app"
        project = load_project(nested)
        self.assertEqual(project.root, self.repo.resolve())
        self.assertEqual(project.config_path, config.resolve())

    def test_missing_project_config_and_non_git_directory(self):
        for path in (self.repo, self.root, self.root / "missing"):
            with self.subTest(path=path), self.assertRaises(TaskError):
                load_project(path)

    def test_invalid_requests_do_not_start_workflow(self):
        orchestrator = Orchestrator(load_project(self.repo, self.config))
        with self.assertRaises(TaskError):
            orchestrator.run(RunRequest(self.card, rerun=True, continue_pr=True))
        with self.assertRaises(TaskError):
            orchestrator.publish(RunRequest(self.card, local_only=True))
        with self.assertRaises(TaskError):
            orchestrator.review(ReviewRequest("branch", ""))
        self.assertFalse((self.root / "runs").exists())

    def test_cli_cwd_and_project_and_relative_paths(self):
        config = self.repo / ".dev-task/config.json"
        config.parent.mkdir()
        config.write_text(self.config.read_text())
        self.g(self.repo, "branch", "main")
        self.g(self.repo, "push", "origin", "main")
        self.write_card(issue="standalone_task", base="main")
        command = [os.environ["DEV_TASK_TEST_ENTRYPOINT"]] if "DEV_TASK_TEST_ENTRYPOINT" in os.environ else [sys.executable, str(workflow.SCRIPT)]
        first = subprocess.run([*command, "run", "../task.md", "--local-only"], cwd=self.repo,
                               env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = subprocess.run([*command, "report", "task.md", "--project", "repo",
                                 "--config", "repo/.dev-task/config.json"], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        state = json.loads(next((self.root / "runs").glob("*/standalone_task/state.json")).read_text())
        self.assertEqual(state["repo"], str(self.repo.resolve()))
        self.assertTrue(Path(state["worktree"], ".git").exists())

    def test_cli_runs_issue_from_another_directory(self):
        self.cfg["tasks_root"] = "repo/tasks"
        self.write_config()
        self.write_card(issue="KRMA-1111")
        card = self.repo / "tasks/KRMA-1111/task.md"
        card.parent.mkdir(parents=True)
        self.card.rename(card)

        result = subprocess.run(
            [sys.executable, str(workflow.SCRIPT), "run", "KRMA-1111", "--local-only",
             "--project", str(self.repo), "--config", str(self.config)],
            cwd=self.bin, env=self.env, capture_output=True, text=True, timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved_card = next((self.root / "runs").glob("*/KRMA-1111/task.md"))
        self.assertEqual(saved_card.read_bytes(), card.read_bytes())

    def test_cli_issue_errors_before_creating_worktree(self):
        command = [sys.executable, str(workflow.SCRIPT), "run", "KRMA-1111",
                   "--project", str(self.repo), "--config", str(self.config)]
        card = self.root / "tasks/KRMA-1111/task.md"

        for case in ("missing setting", "missing folder", "missing file"):
            with self.subTest(case=case):
                if case == "missing folder":
                    self.cfg["tasks_root"] = "tasks"
                    self.write_config()
                if case == "missing file":
                    card.parent.mkdir(parents=True)
                result = subprocess.run(command, cwd=self.bin, env=self.env, capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                expected = "Укажите tasks_root" if case == "missing setting" else str(card.resolve())
                self.assertIn(expected, result.stderr)
                self.assertFalse((self.root / "worktrees").exists())
                self.assertFalse((self.root / "runs").exists())

    def test_environment_and_observers_are_nested_and_reset(self):
        outer, inner = [], []
        before = dict(os.environ)
        with ExecutionContext("outer", outer.append, build_environment({"android_home": "/sdk", "env": {"X": "outer"}})).activate():
            emit(kind="test")
            self.assertEqual(capture([sys.executable, "-c", "import os; print(os.environ['X'])"], self.repo).stdout.strip(), "outer")
            with ExecutionContext("inner", inner.append, {**os.environ, "X": "inner"}).activate():
                emit(kind="test")
            emit(kind="test")
        emit(kind="ignored")
        self.assertEqual([e.operation for e in outer], ["outer", "outer"])
        self.assertEqual([e.operation for e in inner], ["inner"])
        self.assertEqual(dict(os.environ), before)
        with patch.dict(os.environ, {"ANDROID_HOME": "/parent-sdk"}):
            self.assertEqual(build_environment({"android_home": "/legacy-sdk"})["ANDROID_HOME"], "/parent-sdk")
            self.assertEqual(build_environment({"android_home": "/legacy-sdk", "env": {"ANDROID_HOME": "/explicit-sdk"}})["ANDROID_HOME"], "/explicit-sdk")

    def test_failure_resets_context_and_preserves_checkpoint(self):
        events = []
        self.env["FAIL_STAGE"] = "coding"
        project = load_project(self.repo, self.config)
        with patch.dict(os.environ, self.env, clear=True):
            before = dict(os.environ)
            with self.assertRaises(TaskError):
                Orchestrator(project, on_event=events.append).run(RunRequest(self.card, local_only=True))
            self.assertEqual(dict(os.environ), before)
        state = json.loads(next((self.root / "runs").glob("*/*/state.json")).read_text())
        self.assertIn("last_error", state)
        self.assertTrue(Path(state["worktree"]).is_dir())
        self.assertEqual(events[-1].status, "failed")
        count = len(events)
        emit(kind="outside")
        self.assertEqual(len(events), count)

    def test_legacy_worktree_path_is_reused(self):
        orchestrator = Orchestrator(load_project(self.repo, self.config))
        with patch.dict(os.environ, self.env, clear=True):
            result = orchestrator.run(RunRequest(self.card, local_only=True))
            old_path = self.root / "worktrees" / "KRMA-1234"
            self.g(self.repo, "worktree", "move", str(result.worktree), str(old_path))
            state = json.loads(result.state_path.read_text())
            state["worktree"] = str(old_path.resolve())
            result.state_path.write_text(json.dumps(state))
            resumed = orchestrator.run(RunRequest(self.card, local_only=True))
        self.assertEqual(resumed.worktree, old_path.resolve())
