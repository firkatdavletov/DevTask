"""Russian commit subjects, generated through a fake Codex CLI and real local Git."""
import json
from pathlib import Path
import sys
import unittest

import test_workflow as workflow


COMMIT_CODEX = r'''
import json, os, sys
from pathlib import Path
if "app-server" in sys.argv:
    sys.exit(0)
schema = json.loads(Path(sys.argv[sys.argv.index("--output-schema") + 1]).read_text())
if "subject" in schema["properties"]:
    root = Path(os.environ["TEST_ROOT"])
    prompt = sys.stdin.read()
    with (root / "commit-calls.jsonl").open("a") as stream:
        stream.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt}) + "\n")
    if os.environ.get("FAIL_COMMIT_MODEL"):
        sys.exit(9)
    if os.environ.get("MUTATE_COMMIT"):
        Path("app/name.txt").write_text("unexpected change\n")
    subject = os.environ.get("COMMIT_SUBJECT", "Добавил обработку ошибки на экране подачи")
    Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps({"status": "generated", "subject": subject}))
    print(json.dumps({"type": "thread.started", "thread_id": "commit-session"}))
    print(json.dumps({"type": "turn.started"}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 10, "cached_input_tokens": 0}}))
    sys.exit(0)
'''


class CommitMessageTest(unittest.TestCase):
    g = workflow.WorkflowTest.g
    write_config = workflow.WorkflowTest.write_config
    write_card = workflow.WorkflowTest.write_card
    run_task = workflow.WorkflowTest.run_task
    calls = workflow.WorkflowTest.calls

    def setUp(self):
        workflow.WorkflowTest.setUp(self)
        self.write_card(issue="KRMA-10003-add-username-validation-error")
        executable = self.bin / "codex"
        executable.write_text("#!" + sys.executable + "\n" + COMMIT_CODEX + workflow.FAKE_CODEX)

    def state(self):
        return json.loads(next((self.root / "runs").glob("*/*/state.json")).read_text())

    def commit_calls(self):
        path = self.root / "commit-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def failing_hook(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.g(self.repo, "config", "core.hooksPath", str(hooks))
        return hook

    def test_russian_subject_uses_luna_and_only_jira_number(self):
        self.cfg["models"] = {"luna": "test-luna"}
        self.cfg["codex_args"] = ["--add-dir", str(self.root / "cache")]
        self.write_config()
        self.run_task("run", "--local-only")
        self.assertFalse(self.commit_calls())
        self.run_task("publish")
        state = self.state()
        expected = "KRMA-10003 Добавил обработку ошибки на экране подачи"
        self.assertEqual(self.g(Path(state["worktree"]), "log", "-1", "--format=%s"), expected)
        self.assertEqual(state["commit_message"], expected)
        call = self.commit_calls()[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "test-luna")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("model_reasoning_effort=low", argv)
        self.assertNotIn("--add-dir", argv)
        diff = next((self.root / "runs").glob("*/*/attempt-*/commit-message-*/checked.diff"))
        self.assertIn("+new name", diff.read_text())
        report = json.loads(next((self.root / "runs").glob("*/*/report.json")).read_text())
        self.assertEqual(report["stages"]["commit_message"]["tokens"]["total_tokens"], 30)
        self.run_task("publish")
        self.assertEqual(len(self.commit_calls()), 1)

    def test_failed_commit_reuses_message_on_retry(self):
        hook = self.failing_hook()
        hook.write_text("#!/bin/sh\necho 'hook stdout'\necho 'hook stderr' >&2\nexit 1\n")
        failed = self.run_task(success=False)
        log = Path(self.state()["last_attempt"]) / "commit.log"
        self.assertIn(str(log), failed.stderr)
        self.assertIn("hook stdout", log.read_text())
        self.assertIn("hook stderr", log.read_text())
        self.assertNotIn("hook stderr", failed.stderr)
        self.assertEqual(self.g(self.repo, "config", "core.hooksPath"), str(hook.parent))
        self.assertNotIn("commit_sha", self.state())
        self.assertEqual(len(self.commit_calls()), 1)
        hook.write_text("#!/bin/sh\nexit 0\n")
        self.run_task("publish")
        self.assertEqual(len(self.commit_calls()), 1)
        self.assertEqual(self.state()["phase"], "published")

    def repository_hooks(self, *, fail=False, mutate=False):
        hooks = self.repo / "githooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text('''#!/bin/sh
expected="$(git rev-parse --show-toplevel)/githooks"
actual="$(git config --get core.hooksPath)"
if [ "$actual" != "$expected" ]; then
    echo 'Неправильный путь к директории git хуков.' >&2
    exit 1
fi
echo "Verified worktree hooks: $actual"
''' + ("echo 'Hook rejected commit' >&2\nexit 1\n" if fail else
       "echo changed > app/name.txt\ngit add app/name.txt\n" if mutate else "exit 0\n"))
        hook.chmod(0o755)
        self.g(self.repo, "add", "githooks")
        self.g(self.repo, "commit", "-m", "Add repository hooks")
        self.g(self.repo, "push", "origin", "develop")
        self.g(self.repo, "config", "core.hooksPath", str(hooks))
        return hooks

    def test_repository_hooks_use_worktree_path_without_changing_shared_config(self):
        hooks = self.repository_hooks()
        self.run_task()
        state = self.state()
        worktree = Path(state["worktree"])
        log = (Path(state["last_attempt"]) / "commit.log").read_text()
        self.assertIn("Verified worktree hooks: " + str(worktree / "githooks"), log)
        self.assertEqual(state["phase"], "published")
        self.assertEqual(self.g(self.repo, "config", "core.hooksPath"), str(hooks))
        self.assertEqual(self.g(worktree, "config", "core.hooksPath"), str(hooks))

    def test_relocated_repository_hook_failure_stops_publication(self):
        hooks = self.repository_hooks(fail=True)
        failed = self.run_task(success=False)
        state = self.state()
        log = Path(state["last_attempt"]) / "commit.log"
        self.assertIn("Hook rejected commit", log.read_text())
        self.assertIn(str(log), failed.stderr)
        self.assertNotIn("commit_sha", state)
        self.assertFalse((self.root / "github-prs.json").exists())
        self.assertEqual(self.g(self.repo, "config", "core.hooksPath"), str(hooks))

    def test_relocated_hook_cannot_publish_a_changed_checked_snapshot(self):
        self.repository_hooks(mutate=True)
        self.assertIn("Git hook изменил проверенный снимок", self.run_task(success=False).stderr)
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_changed_checked_code_regenerates_message(self):
        hook = self.failing_hook()
        self.run_task(success=False)
        previous_tree = self.state()["commit_message_tree"]
        (Path(self.state()["worktree"]) / "app/extra.txt").write_text("Extra change\n")
        self.run_task("run", "--local-only")
        hook.write_text("#!/bin/sh\nexit 0\n")
        self.run_task("publish")
        self.assertEqual(len(self.commit_calls()), 2)
        self.assertNotEqual(self.state()["commit_message_tree"], previous_tree)

    def test_invalid_subject_stops_before_commit(self):
        self.run_task("run", "--local-only")
        for subject in ("", "Added validation", "Добавил ошибку\nВторая строка",
                        "Добавил ошибку\u2028Вторая строка", "KRMA-10003 Добавил ошибку"):
            with self.subTest(subject=subject):
                self.env["COMMIT_SUBJECT"] = subject
                self.assertIn("Сообщение коммита:", self.run_task("publish", success=False).stderr)
                self.assertNotIn("commit_sha", self.state())
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_model_failure_has_no_fallback_commit(self):
        self.env["FAIL_COMMIT_MODEL"] = "1"
        self.run_task(success=False)
        self.assertNotIn("commit_sha", self.state())
        self.assertNotIn("commit_message", self.state())
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_generation_cannot_modify_checked_code(self):
        self.env["MUTATE_COMMIT"] = "1"
        self.assertIn("Проверки изменили", self.run_task(success=False).stderr)
        self.assertNotIn("commit_sha", self.state())
        self.assertNotIn("commit_message", self.state())


if __name__ == "__main__":
    unittest.main()
