"""Real local Git integration; Codex, GitHub, and displayed remote URLs are fakes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
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
stage = "commit_message" if "subject" in schema["properties"] else "planning" if "plan" in schema["properties"] else "review" if "findings" in schema["properties"] else "coding"
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
if os.environ.get("FAIL_FIX") and stage == "coding" and "Исправь замечания" in prompt:
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
result_path = Path(argv[argv.index("-o") + 1])
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
        self.bin.mkdir()
        for name, body in (("git", FAKE_GIT), ("gh", FAKE_GH), ("codex", FAKE_CODEX)):
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(body))
            path.chmod(0o755)
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
        self.assertEqual(state["session_id"], "session-1")
        self.assertEqual(state["checks"][0]["exit_code"], 0)
        self.assertEqual(self.g(self.root, "--git-dir", str(self.remote), "rev-parse", "feature/KRMA-1234"), state["commit_sha"])
        self.run_task()
        calls = (self.root / "calls").read_text().splitlines()
        self.assertEqual(calls.count("codex"), 1)
        self.assertEqual(calls.count("gh pr create"), 1)
        self.assertEqual(self.g(self.repo, "branch", "--show-current"), "develop")
        self.assertEqual((self.repo / "app/name.txt").read_text(), "old name\n")

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
        self.assertEqual(self.report()["status"], "failed")
        self.env.pop("DISPLAY_REMOTE")
        self.run_task("run", "--local-only")
        self.assertEqual(len(self.report()["invocations"]), 2)

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
        self.assertEqual(self.report()["tokens"]["total_tokens"], 110 * len(calls))

    def test_profile_trivial(self):
        self.assert_profile("TrIvIaL", [("coding", "gpt-6-luna", "low")])
        report = self.report()
        self.assertTrue(report["stages"]["planning"]["skipped"])
        self.assertEqual(report["stages"]["planning"]["tokens"]["total_tokens"], 0)
        self.assertFalse(report["quality"][0]["assessed"])

    def test_profile_simple(self):
        self.assert_profile("simple", [("coding", "gpt-6-luna", "medium"), ("review", "gpt-6.1-sol", "low")])

    def test_profile_medium_plan_file(self):
        self.assert_profile("medium", [("planning", "gpt-6.1-sol", "medium"),
                                       ("coding", "gpt-6.1-sol", "medium"), ("review", "gpt-6-astra", "low")])
        state = self.state()
        plan = Path(state["cycles"][0]["plan_path"])
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
        self.run_task("run", "--local-only", "--rerun", "--no-skip-planning", "--no-skip-review")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "planning", "coding", "review"])
        self.assertEqual(self.report()["manual_reruns"], 1)

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
        self.assertEqual([c["stage"] for c in calls], ["planning", "coding", "review", "coding", "review", "coding", "review"])
        self.assertIn("Исправь замечания", calls[3]["prompt"])
        report = self.report()
        self.assertEqual((report["fixes_started"], report["fixes_completed"]), (2, 2))
        quality = report["quality"][0]
        self.assertEqual(quality["first"]["P1"], 1)
        self.assertEqual(quality["first"]["P2"], 1)
        self.assertEqual(quality["last"]["P1"], 0)
        self.assertEqual(quality["last"]["P2"], 0)
        self.assertEqual(quality["last"]["P3"], 1)
        self.assertEqual(len(quality["rounds"]), 3)
        self.assertEqual(report["stages"]["coding"]["tokens"]["total_tokens"], 330)
        self.assertEqual(report["tokens"]["total_tokens"], 770)
        self.assertGreaterEqual(report["active_seconds"], sum(r["seconds"] for r in report["sessions"]))

    def test_default_three_fixes_limit_persists(self):
        self.write_card(complexity="simple")
        self.env["REVIEW_ROUNDS"] = json.dumps([["P2"]] * 10)
        result = self.run_task(success=False)
        self.assertIn("лимит исправлений", result.stderr)
        self.assertEqual(len(self.calls()), 8)
        self.assertEqual(self.report()["fixes_started"], 3)
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
        self.assertEqual((self.report()["fixes_started"], self.report()["fixes_completed"]), (1, 0))
        self.run_task(success=False)
        self.assertEqual(len(self.calls()), 3)

    def test_p0_blocks_publication_without_fix_budget(self):
        self.write_card(complexity="simple")
        self.cfg["max_fix_iterations"] = 0
        self.write_config()
        self.env["REVIEW_ROUNDS"] = '[["P0"]]'
        self.run_task(success=False)
        self.assertEqual(self.report()["quality"][0]["last"]["P0"], 1)
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_failed_review_resumes_without_repeating_other_stages(self):
        self.write_card(complexity="medium")
        self.env.update(FAIL_STAGE="review", FAIL_ONCE="1", PARTIAL_USAGE="1")
        self.run_task(success=False)
        report = self.report()
        self.assertFalse(report["tokens"]["complete"])
        self.assertEqual(report["tokens"]["total_tokens"], 330)
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["planning", "coding", "review", "review"])
        self.assertEqual(self.report()["tokens"]["total_tokens"], 440)
        self.assertEqual(self.report()["status"], "local_complete")

    def test_failed_planning_stops_before_coding(self):
        self.write_card(complexity="medium")
        self.env.update(FAIL_STAGE="planning", FAIL_ONCE="1")
        self.run_task(success=False)
        self.assertEqual([c["stage"] for c in self.calls()], ["planning"])
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["planning", "planning", "coding", "review"])

    def test_completed_session_checkpoint_recovers_without_duplicate(self):
        self.write_card(complexity="medium")
        self.env["FAIL_STAGE"] = "review"
        self.run_task(success=False)
        state = self.state()
        state["cycles"][0]["next_stage"] = "coding"
        # Mimic a crash after recording coding success, before advancing to checks.
        coding = next(r for r in state["sessions"] if r["stage"] == "coding")
        coding["snapshot"] = state["cycles"][0]["checked_snapshot"]
        self.save_state(state)
        self.env.pop("FAIL_STAGE")
        self.run_task("run", "--local-only")
        self.assertEqual(sum(c["stage"] == "coding" for c in self.calls()), 1)

    def test_readonly_planning_mutation_requires_rerun(self):
        self.write_card(complexity="medium")
        self.env["MUTATE_STAGE"] = "planning"
        self.assertIn("изменил исходники", self.run_task(success=False).stderr)
        self.env.pop("MUTATE_STAGE")
        self.assertIn("--rerun", self.run_task(success=False).stderr)
        self.assertEqual(len(self.calls()), 1)
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual(self.state()["phase"], "verified")

    def test_readonly_review_mutation_does_not_publish(self):
        self.write_card(complexity="simple")
        self.env["MUTATE_STAGE"] = "review"
        self.assertIn("изменил исходники", self.run_task(success=False).stderr)
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_blocked_review_stops(self):
        self.write_card(complexity="simple")
        self.env["BLOCK_STAGE"] = "review"
        self.assertIn("Stage blocked", self.run_task(success=False).stderr)
        self.assertFalse(self.report()["quality"][0]["assessed"])
        self.assertFalse((self.root / "github-prs.json").exists())

    def test_malformed_planning_result_stops(self):
        self.write_card(complexity="medium")
        self.env["MALFORMED_STAGE"] = "planning"
        self.assertIn("Некорректный результат", self.run_task(success=False).stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.report()["status"], "failed")

    def test_review_timeout_report_and_resume(self):
        self.write_card(complexity="simple")
        self.cfg["codex_timeout_seconds"] = 1
        self.write_config()
        self.env["TIMEOUT_STAGE"] = "review"
        self.run_task(success=False)
        self.assertEqual(self.report()["sessions"][-1]["exit_code"], 124)
        self.assertFalse(self.report()["tokens"]["complete"])
        self.env.pop("TIMEOUT_STAGE")
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review", "review"])

    def test_interruption_saves_report(self):
        self.env["INTERRUPT_STAGE"] = "coding"
        result = self.run_task(success=False)
        self.assertEqual(result.returncode, 130)
        self.assertEqual(self.report()["status"], "interrupted")
        self.assertEqual(self.report()["sessions"][-1]["status"], "interrupted")

    def test_unknown_usage_and_repeat_do_not_double_count(self):
        self.env["NO_USAGE"] = "1"
        self.run_task("run", "--local-only")
        report = self.report()
        self.assertFalse(report["tokens"]["complete"])
        self.assertFalse(report["tokens"]["known"])
        self.run_task("run", "--local-only")
        self.assertEqual(len(self.report()["sessions"]), 1)
        self.assertEqual(len(self.calls()), 1)

    def test_partial_usage_preserves_known_fields(self):
        self.env["MISSING_CACHE_USAGE"] = "1"
        self.run_task("run", "--local-only")
        usage = self.report()["tokens"]
        self.assertEqual(usage["total_tokens"], 110)
        self.assertTrue(usage["known"])
        self.assertFalse(usage["complete"])

    def test_tokens_cached_and_publish_dont_double_count(self):
        self.write_card(complexity="medium")
        self.run_task("run", "--local-only")
        before = self.report()
        self.assertEqual(before["tokens"]["input_tokens"], 300)
        self.assertEqual(before["tokens"]["cached_input_tokens"], 240)
        self.assertEqual(before["tokens"]["output_tokens"], 30)
        self.assertEqual(before["tokens"]["total_tokens"], 330)
        self.run_task("publish")
        after = self.report()
        self.assertEqual(after["tokens"], before["tokens"])
        self.assertGreater(after["active_seconds"], before["active_seconds"])
        body = next((self.root / "runs").glob("*/KRMA-1234/pr-body.md")).read_text()
        self.assertNotIn("Токены", body)

    def test_manual_change_after_review_requires_new_review(self):
        self.write_card(complexity="simple")
        self.run_task("run", "--local-only")
        worktree = Path(self.state()["worktree"])
        (worktree / "app/extra.txt").write_text("manual change")
        self.assertIn("Код изменился после review", self.run_task("publish", success=False).stderr)
        self.assertEqual(len(self.calls()), 2)
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review", "review"])
        self.run_task("publish")
        self.assertEqual(len(self.calls()), 3)

    def test_failed_check_resume_uses_coding_result(self):
        self.write_card(complexity="simple")
        self.cfg["checks"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(7)"]
        self.write_config()
        self.run_task(success=False)
        self.cfg["checks"][0]["argv"] = [sys.executable, "-c", "pass"]
        self.write_config()
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review"])

    def test_legacy_uncommitted_requires_rerun(self):
        self.run_task("run", "--local-only")
        state = self.state()
        for key in ("schema_version", "cycles", "sessions"):
            state.pop(key)
        self.save_state(state)
        self.assertIn("Старое незавершённое", self.run_task(success=False).stderr)
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual(self.state()["schema_version"], 2)
        self.assertTrue(self.report()["legacy_metrics_unavailable"])

    def test_legacy_committed_stops_but_published_returns_pr(self):
        self.env["GH_LOST_RESPONSE"] = "1"
        self.run_task(success=False)
        state = self.state()
        state.pop("schema_version")
        self.save_state(state)
        self.assertIn("Старое закоммиченное", self.run_task("publish", success=False).stderr)
        state["phase"] = "published"
        self.save_state(state)
        self.run_task()
        self.assertEqual(len(self.calls()), 1)

    def test_failed_coding_resumes_without_replanning(self):
        self.write_card(complexity="medium")
        self.env.update(FAIL_STAGE="coding", FAIL_ONCE="1")
        self.run_task(success=False)
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["planning", "coding", "coding", "review"])

    def test_malformed_review_result_stops(self):
        self.write_card(complexity="simple")
        self.env["MALFORMED_STAGE"] = "review"
        self.assertIn("Некорректный результат", self.run_task(success=False).stderr)
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

    def test_reused_session_id_is_rejected(self):
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
        self.assertTrue(self.state()["cycles"][0]["requires_rerun"])

    def test_skip_flags_work_independently(self):
        self.write_card(complexity="medium")
        self.run_task("run", "--local-only", "--skip-planning")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review"])
        self.run_task("run", "--local-only", "--rerun", "--no-skip-planning", "--skip-review")
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
