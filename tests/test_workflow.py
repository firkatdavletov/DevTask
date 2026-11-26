"""Real local Git integration; Codex, GitHub, and displayed remote URLs are fakes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "dev-task"
REAL_GIT = shutil.which("git")

FAKE_CODEX = r'''
import json, os, sys, time
from pathlib import Path
root = Path(os.environ["TEST_ROOT"])
argv = sys.argv
if "app-server" in argv:
    sys.exit(0)
schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
result_path = Path(argv[argv.index("-o") + 1])
if "subject" in schema["properties"]:
    stage = "commit_message"
elif "plan" in schema["properties"]:
    stage = "planning"
elif "findings" in schema["properties"]:
    stage = "review"
elif (result_path.parent / "review-failure.json").exists():
    stage = "review_fix"
elif (result_path.parent / "check-failure.json").exists():
    stage = "check_fix"
else:
    stage = "coding"
prompt = sys.stdin.read()
calls_path = root / "codex-calls.jsonl"
calls = [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.exists() else []
number = len(calls) + 1
call = {"stage": stage, "model": argv[argv.index("--model") + 1],
        "effort": next(v for v in argv if v.startswith("model_reasoning_effort=")), "sandbox": argv[argv.index("--sandbox") + 1],
        "session_id": "session-" + str(number), "prompt": prompt, "argv": argv[1:]}
if os.environ.get("FORCE_SESSION_ID"):
    call["session_id"] = os.environ["FORCE_SESSION_ID"]
with calls_path.open("a") as f:
    f.write(json.dumps(call) + "\n")
with (root / "calls").open("a") as f:
    f.write("codex\n")
print(json.dumps({"type": "thread.started", "thread_id": call["session_id"]}), flush=True)
if os.environ.get("REQUIRE_SETUP"):
    assert Path("local.properties").read_text() == "1"
if os.environ.get("INTERRUPT_STAGE") == stage:
    import signal
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(3)
if os.environ.get("TIMEOUT_STAGE") == stage:
    time.sleep(3)
if os.environ.get("FAIL_FIX") and stage == "review_fix":
    sys.exit(9)
if os.environ.get("FAIL_STAGE") == stage:
    marker = root / ("failed-" + stage)
    if not os.environ.get("FAIL_ONCE") or not marker.exists():
        marker.touch()
        if os.environ.get("PARTIAL_USAGE"):
            print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10}}))
        print(json.dumps({"type": "turn.failed", "error": {"message": "test failure"}}))
        sys.exit(8)
for line in prompt.splitlines():
    if line.startswith("План реализации в файле: "):
        assert "Test plan" in Path(line.split(": ", 1)[1]).read_text()
    if line.startswith("Исправь замечания последнего review из файла: "):
        assert json.loads(Path(line.split(": ", 1)[1]).read_text())["findings"]
if stage == "commit_message":
    result = {"status": "generated", "subject": "Добавил редактирование имени пользователя"}
elif stage == "planning":
    result = {"status": "planned", "summary": "План", "plan": "# Test plan\nImplement name editing.", "blockers": []}
elif stage == "review":
    review_number = sum(c["stage"] == "review" for c in calls)
    rounds = json.loads(os.environ.get("REVIEW_ROUNDS", "[]"))
    priorities = rounds[review_number] if review_number < len(rounds) else []
    findings = [{"priority": p, "file": "app/name.txt", "line": 1, "explanation": "Fix " + p} for p in priorities]
    result = {"status": "reviewed", "summary": "Review", "findings": findings, "blockers": []}
else:
    result = {"status": "implemented", "summary": "Реализована задача", "manual_checks": ["Проверить UI"], "verification_limits": [], "blockers": []}
    behavior = os.environ.get("CODEX_BEHAVIOR", "ok")
    if behavior == "blocked":
        result.update(status="needs_input", blockers=["Какой API использовать?"])
    elif behavior == "branch":
        import subprocess
        subprocess.run([os.environ["REAL_GIT"], "switch", "-c", "wrong-branch"], check=True)
    elif behavior == "secret":
        Path("app/secret.jks").write_text("do not publish")
    else:
        Path("app/name.txt").write_text("new name\n")
if os.environ.get("MUTATE_STAGE") == stage:
    Path("app/name.txt").write_text("changed by " + stage)
if os.environ.get("MUTATE_INDEX_STAGE") == stage:
    import subprocess
    subprocess.run([os.environ["REAL_GIT"], "reset", "HEAD", "--", "app/name.txt"], check=True, stdout=subprocess.DEVNULL)
if os.environ.get("BLOCK_STAGE") == stage:
    result.update(status="blocked", blockers=["Stage blocked"])
if os.environ.get("MALFORMED_STAGE") == stage:
    result = {"unexpected": True}
result_path.write_text(json.dumps(result))
if os.environ.get("NO_USAGE"):
    print(json.dumps({"type": "turn.completed"}))
elif os.environ.get("MISSING_CACHE_USAGE"):
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}))
else:
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10}}))
'''

FAKE_GH = r'''
import json, os, sys
from pathlib import Path
root = Path(os.environ["TEST_ROOT"])
with (root / "calls").open("a") as f:
    f.write("gh " + " ".join(sys.argv[1:3]) + "\n")
path = root / "github-prs.json"
prs = json.loads(path.read_text()) if path.exists() else []
if sys.argv[1:3] == ["pr", "list"]:
    head = sys.argv[sys.argv.index("--head") + 1]
    print(json.dumps([pr for pr in prs if pr["headRefName"] == head]))
elif sys.argv[1:3] == ["pr", "create"]:
    pr = {"number": 42, "url": "https://github.test/team/app/pull/42", "state": "OPEN", "isDraft": "--draft" in sys.argv,
          "baseRefName": sys.argv[sys.argv.index("--base") + 1],
          "headRefName": sys.argv[sys.argv.index("--head") + 1], "isCrossRepository": False}
    prs.append(pr)
    path.write_text(json.dumps(prs))
    if os.environ.get("GH_LOST_RESPONSE") and not (root / "lost-once").exists():
        (root / "lost-once").touch()
        sys.exit(1)
    print(pr["url"])
else:
    sys.exit(2)
'''

FAKE_GIT = r'''
import os, sys
if sys.argv[1:3] == ["remote", "get-url"]:
    print(os.environ.get("DISPLAY_REMOTE", "git@github.test:team/app.git"))
else:
    os.execv(os.environ["REAL_GIT"], [os.environ["REAL_GIT"], *sys.argv[1:]])
'''


def install_fake_commands(directory):
    directory.mkdir()
    for name, body in (("git", FAKE_GIT), ("gh", FAKE_GH), ("codex", FAKE_CODEX)):
        path = directory / name
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(body))
        path.chmod(0o755)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.repo.mkdir()
        self.g(self.root, "init", "--bare", str(self.remote))
        self.g(self.repo, "init", "-b", "develop")
        self.g(self.repo, "config", "user.name", "Test Developer")
        self.g(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "app").mkdir()
        (self.repo / "app/name.txt").write_text("old name\n")
        (self.repo / ".gitignore").write_text("local.properties\n")
        self.g(self.repo, "add", ".")
        self.g(self.repo, "commit", "-m", "Initial")
        self.g(self.repo, "remote", "add", "origin", str(self.remote))
        self.g(self.repo, "push", "-u", "origin", "develop")
        self.bin = self.root / "bin"
        install_fake_commands(self.bin)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "REAL_GIT": REAL_GIT, "TEST_ROOT": str(self.root),
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.config = self.root / "config.json"
        self.cfg = {"repo": str(self.repo), "github_repo": "github.test/team/app",
                    "worktrees_root": str(self.root / "worktrees"), "runs_root": str(self.root / "runs"),
                    "checks": [{"name": "verify file", "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('app/name.txt').read_text() == 'new name\\n'"]}],
                    "allow_paths": ["app/**"], "deny_paths": ["**/*.jks"]}
        self.write_config()
        self.card = self.root / "task.md"
        self.write_card()

    def g(self, cwd, *args):
        result = subprocess.run([REAL_GIT, *args], cwd=cwd, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def write_config(self):
        self.config.write_text(json.dumps(self.cfg))

    def write_card(self, issue="KRMA-1234", base="develop", complexity="trivial"):
        self.card.write_text(f"---\nissue: {issue}\nbase: {base}\ntitle: Test task\ncomplexity: {complexity}\n---\nImplement name editing.\n")

    def run_task(self, command="run", *flags, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), command, str(self.card), "--config", str(self.config), "--project", str(self.repo), *flags],
                                env=self.env, capture_output=True, text=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def state(self):
        return json.loads(next((self.root / "runs").glob("*/KRMA-1234/state.json")).read_text())

    def calls(self):
        path = self.root / "codex-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def report(self):
        return json.loads(next((self.root / "runs").glob("*/KRMA-1234/report.json")).read_text())

    def save_state(self, value):
        next((self.root / "runs").glob("*/KRMA-1234/state.json")).write_text(json.dumps(value))

    def test_full_flow_and_duplicate_run(self):
        self.run_task()
        state = self.state()
        self.assertEqual(state["phase"], "published")
        self.assertEqual([call["stage"] for call in self.calls()], ["coding", "commit_message"])
        self.assertEqual(state["checks"][0]["exit_code"], 0)
        self.assertEqual(self.g(self.root, "--git-dir", str(self.remote), "rev-parse", "feature/KRMA-1234"), state["commit_sha"])
        self.run_task()
        calls = (self.root / "calls").read_text().splitlines()
        self.assertEqual(calls.count("codex"), 2)
        self.assertEqual(calls.count("gh pr create"), 1)
        self.assertEqual(self.g(self.repo, "branch", "--show-current"), "develop")
        self.assertEqual((self.repo / "app/name.txt").read_text(), "old name\n")

    def test_parallel_runs_share_three_slots_and_reject_duplicate_issue(self):
        barrier = """
            import os, time
            from pathlib import Path
            directory = Path(os.environ["TEST_ROOT"])
            (directory / "check-started").touch()
            deadline = time.monotonic() + 30
            while not (directory / "release-check").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Test did not release check")
                time.sleep(0.02)
        """
        self.cfg["checks"].append({"name": "wait for other runs",
                                   "argv": [sys.executable, "-c", textwrap.dedent(barrier)]})
        self.write_config()

        linked = self.root / "linked"
        self.g(self.repo, "worktree", "add", "--detach", str(linked), "HEAD")
        issues = ("DEMO-1", "DEMO-2", "DEMO-3", "DEMO-4")
        for issue in issues:
            (self.root / issue).mkdir()
            self.card = self.root / issue / "task.md"
            self.write_card(issue=issue)

        def cli(issue, command="run", cwd=self.repo):
            directory = self.root / issue
            args = [sys.executable, str(SCRIPT), command, str(directory / "task.md"), "--config", str(self.config)]
            return args, {"cwd": cwd, "env": {**self.env, "TEST_ROOT": str(directory)}, "text": True}

        def rejected(issue, message, command="run"):
            args, kwargs = cli(issue, command, cwd=linked)
            result = subprocess.run(args, **kwargs, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn(message, result.stderr)

        def release(issue):
            (self.root / issue / "release-check").touch()

        def finish(issue, process):
            out, err = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, out + err)
            state_path, = (self.root / "runs").glob(f"*/{issue}/state.json")
            state = json.loads(state_path.read_text())
            self.assertEqual(state["phase"], "published")
            self.assertEqual(self.g(self.root, "--git-dir", str(self.remote), "rev-parse", f"feature/{issue}"),
                             state["commit_sha"])

        processes = {}
        try:
            for issue in issues[:3]:
                args, kwargs = cli(issue)
                processes[issue] = subprocess.Popen(args, **kwargs, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            deadline = time.monotonic() + 15
            for issue, process in processes.items():
                while not (self.root / issue / "check-started").exists():
                    if process.poll() is not None:
                        self.fail(f"{issue} exited early: {process.communicate()}")
                    self.assertLess(time.monotonic(), deadline, f"{issue} did not reach its check")
                    time.sleep(0.02)
            self.assertTrue(all(process.poll() is None for process in processes.values()))

            rejected("DEMO-4", "заняты все 3 слота DevTask")
            rejected("DEMO-1", "Задача DEMO-1 уже выполняется")
            rejected("DEMO-1", "Задача DEMO-1 уже выполняется", command="publish")
            self.assertFalse(list((self.root / "runs").glob("*/DEMO-4")))

            release("DEMO-1")
            finish("DEMO-1", processes["DEMO-1"])
            self.assertTrue(all(processes[issue].poll() is None for issue in issues[1:3]))

            release("DEMO-4")
            args, kwargs = cli("DEMO-4")
            processes["DEMO-4"] = subprocess.Popen(args, **kwargs, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            finish("DEMO-4", processes["DEMO-4"])

            for issue in issues[1:3]:
                release(issue)
            for issue in issues[1:3]:
                finish(issue, processes[issue])
        finally:
            for issue in issues:
                release(issue)
            for process in processes.values():
                if process.poll() is None:
                    try:
                        process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                else:
                    process.communicate()

    def test_local_files_copy_retries_before_setup_and_preserves_resumed_edits(self):
        with (self.repo / ".gitignore").open("a") as ignored:
            ignored.write(".codex/\n.agents/\n")
        self.g(self.repo, "add", ".gitignore")
        self.g(self.repo, "commit", "-m", "Ignore local Codex files")
        self.g(self.repo, "push", "origin", "develop")
        config = self.repo / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('model = "local"\n')
        skill = self.repo / ".agents/skills/test skill/.hidden/helper.sh"
        skill.parent.mkdir(parents=True)
        (skill.parent.parent / "SKILL.md").write_text("# Personal skill\n")
        skill.write_text("#!/bin/sh\nexit 0\n")
        skill.chmod(0o755)
        self.cfg["setup"] = [{"name": "verify copied files", "argv": [sys.executable, "-c",
            "from pathlib import Path; "
            "assert Path('.codex/config.toml').read_text() == 'model = \"local\"\\n'; "
            "assert Path('.agents/skills/test skill/.hidden/helper.sh').stat().st_mode & 0o111"]}]
        self.write_config()
        rsync = self.bin / "rsync"
        rsync.write_text("#!/bin/sh\nexit 7\n")
        rsync.chmod(0o755)

        self.run_task(success=False)
        state = self.state()
        self.assertFalse(state["local_files_copied"])
        self.assertFalse(self.calls())
        self.assertNotIn("setup_completed", state)
        rsync.unlink()

        self.run_task()
        self.assertTrue(self.state()["local_files_copied"])
        worktree = Path(state["worktree"])
        self.assertEqual((worktree / skill.relative_to(self.repo)).read_text(), skill.read_text())
        self.assertEqual(self.g(worktree, "status", "--porcelain"), "")
        copied_config = worktree / ".codex/config.toml"
        copied_config.write_text('model = "edited in worktree"\n')
        self.run_task()
        self.assertEqual(copied_config.read_text(), 'model = "edited in worktree"\n')

    def test_subtask_uses_parent_branch(self):
        self.g(self.repo, "switch", "-c", "feature/KRMA-1233")
        (self.repo / "app/parent.txt").write_text("parent\n")
        self.g(self.repo, "add", ".")
        self.g(self.repo, "commit", "-m", "Parent task")
        parent = self.g(self.repo, "rev-parse", "HEAD")
        self.g(self.repo, "push", "origin", "feature/KRMA-1233")
        self.write_card(issue="AAS-5678", base="feature/KRMA-1233")
        self.run_task()
        prs = json.loads((self.root / "github-prs.json").read_text())
        self.assertEqual(prs[0]["baseRefName"], "feature/KRMA-1233")
        self.assertEqual(self.g(self.root, "--git-dir", str(self.remote), "rev-parse", "feature/AAS-5678^"), parent)

    def test_local_only_then_publish(self):
        self.run_task("run", "--local-only")
        state = self.state()
        self.assertEqual(state["phase"], "verified")
        self.assertNotIn("commit_sha", state)
        self.assertFalse((self.root / "github-prs.json").exists())
        self.run_task("publish")
        self.assertEqual(self.state()["phase"], "published")

    def test_failed_check_does_not_publish(self):
        self.cfg["checks"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(7)"]
        self.write_config()
        self.run_task(success=False)
        self.assertNotIn("commit_sha", self.state())
        self.assertFalse((self.root / "github-prs.json").exists())
        self.assertEqual(self.g(self.root, "--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)", "refs/heads/feature"), "")

    def test_missing_requirements_stop_before_checks(self):
        self.env["CODEX_BEHAVIOR"] = "blocked"
        result = self.run_task(success=False)
        self.assertIn("Какой API использовать?", result.stderr)
        self.assertNotIn("commit_sha", self.state())

    def test_agent_branch_change_stops_publication(self):
        self.env["CODEX_BEHAVIOR"] = "branch"
        result = self.run_task(success=False)
        self.assertIn("изменилась рабочая ветка", result.stderr)
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_forbidden_file_stops_publication(self):
        self.env["CODEX_BEHAVIOR"] = "secret"
        result = self.run_task(success=False)
        self.assertIn("deny_paths", result.stderr)

    def test_check_that_modifies_source_stops_publication(self):
        self.cfg["checks"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; Path('app/name.txt').write_text('changed by check')"]
        self.write_config()
        result = self.run_task(success=False)
        self.assertIn("Проверки изменили", result.stderr)
        self.assertNotIn("commit_sha", self.state())

    def test_lost_pr_response_recovers_without_duplicate(self):
        self.env["GH_LOST_RESPONSE"] = "1"
        self.run_task(success=False)
        self.assertEqual(self.state()["phase"], "committed")
        commit = self.state()["commit_sha"]
        self.cfg["agents"] = {"text": {"model": "next-writer", "effort": "high"}}
        self.write_config()
        self.run_task("publish")
        self.assertEqual(self.state()["commit_sha"], commit)
        self.assertEqual(len(json.loads((self.root / "github-prs.json").read_text())), 1)

    def test_card_edit_requires_explicit_rerun(self):
        self.run_task("run", "--local-only")
        self.card.write_text(self.card.read_text() + "Additional requirement.\n")
        result = self.run_task("publish", success=False)
        self.assertIn("Карточка изменилась", result.stderr)
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual((self.root / "calls").read_text().splitlines().count("codex"), 2)

    def test_wrong_remote_stops_before_agent(self):
        self.env["DISPLAY_REMOTE"] = "git@other.test:team/app.git"
        result = self.run_task(success=False)
        self.assertIn("Git remote", result.stderr)
        self.assertFalse((self.root / "calls").exists())
        self.assertFalse(list((self.root / "runs").glob("*/KRMA-1234/state.json")))
        self.assertFalse(list((self.root / "runs").glob("*/KRMA-1234/report.json")))
        self.env.pop("DISPLAY_REMOTE")
        self.run_task("run", "--local-only")
        self.assertEqual(self.report()["phase"], "verified")
        self.assertEqual([session["stage"] for session in self.report()["sessions"]], ["coding"])

    def test_other_pr_base_is_not_silently_changed(self):
        self.run_task("run", "--local-only")
        (self.root / "github-prs.json").write_text(json.dumps([{
            "number": 99, "url": "https://github.test/team/app/pull/99", "state": "OPEN", "isDraft": True,
            "baseRefName": "feature/KRMA-9999", "headRefName": "feature/KRMA-1234", "isCrossRepository": False
        }]))
        result = self.run_task("publish", success=False)
        self.assertIn("другие ветки", result.stderr)
        self.assertNotIn("commit_sha", self.state())

    def assert_profile(self, complexity, expected):
        self.write_card(complexity=complexity)
        self.run_task("run", "--local-only")
        calls = self.calls()
        actual = [(c["stage"], c["model"], c["effort"].split("=", 1)[1]) for c in calls]
        self.assertEqual(actual, expected)
        self.assertEqual(len({c["session_id"] for c in calls}), len(calls))
        for call in calls:
            self.assertEqual(call["sandbox"], "workspace-write" if call["stage"] == "coding" else "read-only")
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 110 * len(calls))

    def test_profile_trivial(self):
        self.assert_profile("TrIvIaL", [("coding", "gpt-6-luna", "low")])
        report = self.report()
        self.assertEqual(report["planning_status"], "skipped")
        self.assertEqual(report["stages"]["planning"]["tokens"]["total_tokens"], 0)
        self.assertEqual(report["quality_status"], "not_assessed")

    def test_profile_simple(self):
        self.assert_profile("simple", [("coding", "gpt-6-luna", "medium"), ("review", "gpt-6.1-sol", "low")])

    def test_profile_medium_plan_file(self):
        self.assert_profile("medium", [("planning", "gpt-6.1-sol", "medium"),
                                       ("coding", "gpt-6.1-sol", "medium"), ("review", "gpt-6-astra", "low")])
        state = self.state()
        plan = Path(state["plan_path"])
        self.assertTrue(plan.exists())
        self.assertFalse(plan.is_relative_to(Path(state["worktree"])))
        for call in self.calls()[1:]:
            self.assertIn(str(plan), call["prompt"])

    def test_profile_complex(self):
        self.assert_profile("complex", [("planning", "gpt-6-astra", "high"),
                                        ("coding", "gpt-6.1-sol", "high"), ("review", "gpt-6-astra", "medium")])

    def test_complexity_required_before_agent(self):
        self.card.write_text(self.card.read_text().replace("complexity: trivial\n", ""))
        self.assertIn("complexity", self.run_task(success=False).stderr)
        self.write_card(complexity="difficult")
        self.assertIn("complexity", self.run_task(success=False).stderr)
        self.assertFalse(self.calls())
        self.assertFalse((self.root / "worktrees").exists())

    def test_skip_flags_persist_and_can_be_reset(self):
        self.write_card(complexity="medium")
        self.run_task("run", "--local-only", "--skip-planning", "--skip-review")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding"])
        self.run_task("run", "--local-only")
        self.assertEqual(len(self.calls()), 1)
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "planning", "coding", "review"])
        self.assertEqual(len(self.report()["cycle_history"]), 1)

    def test_stage_profile_overrides_and_direct_model_id(self):
        self.write_card(complexity="medium")
        self.cfg["models"] = {"sol": "test-sol"}
        self.cfg["complexity_profiles"] = {"medium": {"coding": {"model": "custom-model", "effort": "high"}, "review": None}}
        self.write_config()
        self.run_task("run", "--local-only")
        self.assertEqual([(c["model"], c["effort"]) for c in self.calls()],
                         [("test-sol", "model_reasoning_effort=medium"), ("custom-model", "model_reasoning_effort=high")])

    def test_invalid_profiles_and_conflicting_args(self):
        invalid = [{"complexity_profiles": {"trivial": {"coding": None}}},
                   {"complexity_profiles": {"hard": {}}}, {"max_fix_iterations": -1},
                   *[{"codex_args": args} for args in (["--model", "other"], ["-mfoo"], ["--sandbox=workspace-write"],
                     ["-c", "model_reasoning_effort=high"], ["-cmodel=other"], ["--config=model=other"],
                     ["--profile", "other"], ["resume", "id"], ["--output-schema", "other"])]]
        original = dict(self.cfg)
        for extra in invalid:
            with self.subTest(extra=extra):
                self.cfg = {**original, **extra}
                self.write_config()
                self.run_task(success=False)
        self.assertFalse(self.calls())

    def test_changed_profile_requires_rerun(self):
        self.run_task("run", "--local-only")
        self.cfg["models"] = {"luna": "another-luna"}
        self.write_config()
        self.assertIn("Профиль", self.run_task("publish", success=False).stderr)
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual(self.calls()[-1]["model"], "another-luna")

    def test_review_fixes_then_passes_and_reports_quality(self):
        self.write_card(complexity="medium")
        self.env["REVIEW_ROUNDS"] = json.dumps([["P1", "P2"], ["P2"], ["P3"]])
        self.run_task("run", "--local-only")
        calls = self.calls()
        self.assertEqual([c["stage"] for c in calls],
                         ["planning", "coding", "review", "review_fix", "review", "review_fix", "review"])
        self.assertIn("Исправь замечания", calls[3]["prompt"])
        report = self.report()
        self.assertEqual((report["review_fix_started"], report["review_fix_completed"]), (2, 2))
        quality = report["quality"][0]
        self.assertEqual((quality["first_p1_p2"], quality["last_p1_p2"]), (2, 0))
        self.assertEqual([[finding["priority"] for finding in review["result"]["findings"]]
                          for review in quality["reviews"]], [["P1", "P2"], ["P2"], ["P3"]])
        self.assertEqual(report["stages"]["coding"]["tokens"]["total_tokens"], 110)
        self.assertEqual(report["stages"]["review_fix"]["tokens"]["total_tokens"], 220)
        self.assertEqual(report["total"]["tokens"]["total_tokens"], 770)

    def test_default_three_fixes_limit_persists(self):
        self.write_card(complexity="simple")
        self.env["REVIEW_ROUNDS"] = json.dumps([["P2"]] * 10)
        result = self.run_task(success=False)
        self.assertIn("лимит исправлений", result.stderr)
        self.assertEqual(len(self.calls()), 8)
        self.assertEqual(self.report()["review_fix_started"], 3)
        self.run_task(success=False)
        self.assertEqual(len(self.calls()), 8)
        self.assertNotIn("commit_sha", self.state())
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_failed_fix_consumes_limit(self):
        self.write_card(complexity="simple")
        self.cfg["max_fix_iterations"] = 1
        self.write_config()
        self.env.update(REVIEW_ROUNDS='[["P1"]]', FAIL_FIX="1")
        self.run_task(success=False)
        report = self.report()
        self.assertEqual((report["review_fix_started"], report["review_fix_completed"]), (1, 0))
        self.run_task(success=False)
        self.assertEqual(len(self.calls()), 3)

    def test_p0_blocks_publication_without_fix_budget(self):
        self.write_card(complexity="simple")
        self.cfg["max_fix_iterations"] = 0
        self.write_config()
        self.env["REVIEW_ROUNDS"] = '[["P0"]]'
        self.run_task(success=False)
        reviews = self.report()["quality"][0]["reviews"]
        self.assertEqual([finding["priority"] for finding in reviews[-1]["result"]["findings"]], ["P0"])
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_failed_review_resumes_without_repeating_other_stages(self):
        self.write_card(complexity="medium")
        self.env.update(FAIL_STAGE="review", FAIL_ONCE="1", PARTIAL_USAGE="1")
        self.run_task(success=False)
        report = self.report()
        self.assertEqual(report["total"]["tokens"]["status"], "partial")
        self.assertEqual(report["total"]["tokens"]["total_tokens"], 330)
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["planning", "coding", "review", "review"])
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 440)
        self.assertEqual(self.report()["phase"], "reviewed")

    def test_failed_planning_stops_before_coding(self):
        self.write_card(complexity="medium")
        self.env.update(FAIL_STAGE="planning", FAIL_ONCE="1")
        self.run_task(success=False)
        self.assertEqual([c["stage"] for c in self.calls()], ["planning"])
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["planning", "planning", "coding", "review"])

    def test_resume_after_interrupted_check_reuses_saved_implementation(self):
        self.write_card(complexity="medium")
        successful_check = self.cfg["checks"][0]["argv"]
        self.cfg["checks"][0]["argv"] = [sys.executable, "-c",
                                       "import os, signal; os.kill(os.getppid(), signal.SIGINT)"]
        self.write_config()
        self.assertEqual(self.run_task(success=False).returncode, 130)
        state = self.state()
        self.assertEqual(state["phase"], "implemented")
        self.assertEqual(state["result"]["status"], "implemented")
        self.assertNotIn("checked_tree", state)
        self.cfg["checks"][0]["argv"] = successful_check
        self.write_config()
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["planning", "coding", "review"])

    def test_readonly_planning_mutation_requires_rerun(self):
        self.write_card(complexity="medium")
        self.env["MUTATE_STAGE"] = "planning"
        self.assertIn("изменил исходники", self.run_task(success=False).stderr)
        self.env.pop("MUTATE_STAGE")
        self.assertIn("--rerun", self.run_task(success=False).stderr)
        self.assertEqual(len(self.calls()), 1)
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual(self.state()["phase"], "reviewed")

    def test_readonly_review_mutation_does_not_publish(self):
        self.write_card(complexity="simple")
        self.env["MUTATE_STAGE"] = "review"
        self.assertIn("изменил исходники", self.run_task(success=False).stderr)
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_blocked_review_stops(self):
        self.write_card(complexity="simple")
        self.env["BLOCK_STAGE"] = "review"
        self.assertIn("Stage blocked", self.run_task(success=False).stderr)
        report = self.report()
        self.assertEqual(report["quality_status"], "not_assessed")
        self.assertEqual(report["quality"][0]["reviews"][-1]["result"]["status"], "blocked")
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_malformed_planning_result_stops(self):
        self.write_card(complexity="medium")
        self.env["MALFORMED_STAGE"] = "planning"
        self.assertIn("некорректный результат planning", self.run_task(success=False).stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.report()["sessions"][-1]["status"], "failed")

    def test_review_timeout_report_and_resume(self):
        self.write_card(complexity="simple")
        self.cfg["codex_timeout_seconds"] = 1
        self.write_config()
        self.env["TIMEOUT_STAGE"] = "review"
        self.run_task(success=False)
        report = self.report()
        self.assertEqual(report["sessions"][-1]["status"], "timed_out")
        self.assertEqual(report["total"]["tokens"]["status"], "partial")
        self.env.pop("TIMEOUT_STAGE")
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review", "review"])

    def test_interruption_saves_report(self):
        self.env["INTERRUPT_STAGE"] = "coding"
        result = self.run_task(success=False)
        self.assertEqual(result.returncode, 130)
        report = self.report()
        self.assertIn("KeyboardInterrupt", report["last_error"])
        self.assertEqual(report["sessions"][-1]["status"], "interrupted")

    def test_unknown_usage_and_repeat_do_not_double_count(self):
        self.env["NO_USAGE"] = "1"
        self.run_task("run", "--local-only")
        report = self.report()
        self.assertEqual(report["total"]["tokens"]["status"], "unknown")
        self.assertIsNone(report["total"]["tokens"]["total_tokens"])
        self.run_task("run", "--local-only")
        self.assertEqual(len(self.report()["sessions"]), 1)
        self.assertEqual(len(self.calls()), 1)

    def test_partial_usage_preserves_known_fields(self):
        self.env["MISSING_CACHE_USAGE"] = "1"
        self.run_task("run", "--local-only")
        usage = self.report()["total"]["tokens"]
        self.assertEqual(usage["total_tokens"], 110)
        self.assertIsNone(usage["cached_input_tokens"])
        self.assertEqual(usage["status"], "partial")

    def test_tokens_cached_and_publish_dont_double_count(self):
        self.write_card(complexity="medium")
        self.run_task("run", "--local-only")
        before = self.report()
        self.assertEqual(before["total"]["tokens"], {"input_tokens": 300, "cached_input_tokens": 240,
                         "output_tokens": 30, "total_tokens": 330, "status": "complete"})
        self.run_task("publish")
        after = self.report()
        self.assertEqual([session["stage"] for session in after["sessions"]], ["planning", "coding", "review", "commit_message"])
        self.assertEqual(after["sessions"][:-1], before["sessions"])
        self.assertEqual(after["total"]["tokens"], {"input_tokens": 400, "cached_input_tokens": 320,
                         "output_tokens": 40, "total_tokens": 440, "status": "complete"})
        body = next((self.root / "runs").glob("*/KRMA-1234/pr-body.md")).read_text()
        self.assertNotIn("Токены", body)

    def test_manual_change_after_review_requires_new_review(self):
        self.write_card(complexity="simple")
        self.run_task("run", "--local-only")
        worktree = Path(self.state()["worktree"])
        (worktree / "app/extra.txt").write_text("manual change")
        self.assertIn("Нет актуального успешного review", self.run_task("publish", success=False).stderr)
        self.assertEqual(len(self.calls()), 2)
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review", "review"])
        self.run_task("publish")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review", "review", "commit_message"])

    def test_failed_check_resume_uses_coding_result(self):
        self.write_card(complexity="simple")
        self.cfg["max_check_fix_iterations"] = 0
        self.cfg["checks"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(7)"]
        self.write_config()
        self.run_task(success=False)
        self.cfg["checks"][0]["argv"] = [sys.executable, "-c", "pass"]
        self.write_config()
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review"])

    def test_legacy_uncommitted_reuses_completed_coding_then_can_rerun(self):
        self.write_card(complexity="medium")
        self.run_task("run", "--local-only", "--skip-planning")
        state = self.state()
        state["workflow_version"] = 2
        state["execution_profile"].pop("planning")
        for key in ("planning_status", "planning_skip_reason", "coding_started"):
            state.pop(key)
        self.save_state(state)
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review"])
        self.assertEqual(self.state()["workflow_version"], 4)
        self.assertEqual(self.state()["planning_skip_reason"], "цикл начат до добавления planning")
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review", "planning", "coding", "review"])
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 550)

    def test_legacy_committed_stops_but_published_returns_pr(self):
        self.env["GH_LOST_RESPONSE"] = "1"
        self.run_task(success=False)
        state = self.state()
        for key in ("workflow_version", "execution_profile", "reviewed_tree"):
            state.pop(key)
        self.save_state(state)
        self.assertIn("Завершите публикацию вручную", self.run_task("publish", success=False).stderr)
        state["phase"] = "published"
        self.save_state(state)
        self.assertIn("Draft PR уже существует", self.run_task().stdout)
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "commit_message"])

    def test_failed_coding_resumes_without_replanning(self):
        self.write_card(complexity="medium")
        self.env.update(FAIL_STAGE="coding", FAIL_ONCE="1")
        self.run_task(success=False)
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["planning", "coding", "coding", "review"])

    def test_malformed_review_result_stops(self):
        self.write_card(complexity="simple")
        self.env["MALFORMED_STAGE"] = "review"
        self.assertIn("некорректный результат review", self.run_task(success=False).stderr)
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_setup_runs_before_planning_and_only_once(self):
        self.write_card(complexity="medium")
        self.cfg["setup"] = [{"name": "prepare", "argv": [sys.executable, "-c",
            "from pathlib import Path; p=Path('local.properties'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"]}]
        self.write_config()
        self.env["REQUIRE_SETUP"] = "1"
        self.run_task("run", "--local-only")
        self.run_task("run", "--local-only")
        self.assertEqual((Path(self.state()["worktree"]) / "local.properties").read_text(), "1")
        self.assertEqual(len(self.calls()), 3)

    def test_coding_rejects_reuse_of_planning_session(self):
        self.write_card(complexity="medium")
        self.env["FORCE_SESSION_ID"] = "same-session"
        self.assertIn("разных сессиях", self.run_task(success=False).stderr)
        self.assertEqual(len(self.calls()), 2)
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_readonly_review_index_change_is_rejected(self):
        self.write_card(complexity="simple")
        self.env["MUTATE_INDEX_STAGE"] = "review"
        self.assertIn("изменил исходники или index", self.run_task(success=False).stderr)
        self.assertEqual((Path(self.state()["worktree"]) / "app/name.txt").read_text(), "new name\n")
        self.assertEqual(self.state()["requires_rerun"], "Review")
        self.env.pop("MUTATE_INDEX_STAGE")
        self.assertIn("--rerun", self.run_task(success=False).stderr)
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review"])
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual(self.state()["phase"], "reviewed")

    def test_skip_flags_work_independently(self):
        self.write_card(complexity="medium")
        self.run_task("run", "--local-only", "--skip-planning")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review"])
        self.run_task("run", "--local-only", "--rerun", "--skip-review")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review", "planning", "coding"])

    def test_git_hook_changes_checked_snapshot_stops_push(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'hook change' > app/name.txt\n")
        hook.chmod(0o755)
        self.g(self.repo, "config", "core.hooksPath", str(hooks))
        self.run_task(success=False)
        self.assertEqual(self.state()["phase"], "committed")
        self.assertFalse((self.root / "github-prs.json").exists())
        self.assertEqual(self.g(self.root, "--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)", "refs/heads/feature"), "")


if __name__ == "__main__":
    unittest.main()
