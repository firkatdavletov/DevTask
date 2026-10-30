"""Check-repair integration tests for the current APP/SS workflow. No remote services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from test_workflow import FAKE_GH, FAKE_GIT

SCRIPT = Path(__file__).resolve().parents[1] / "dev-task"
REAL_GIT = shutil.which("git")

FAKE_CODEX = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(os.environ["TEST_ROOT"])
if "app-server" in sys.argv:
    counter = root / "limit-call-count"
    number = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(number))
    initialized = False
    for line in sys.stdin:
        request = json.loads(line)
        with (root / "rpc-calls.jsonl").open("a") as f:
            f.write(json.dumps(request) + "\n")
        if request["method"] == "initialize":
            print(json.dumps({"id": request["id"], "result": {}}), flush=True)
        elif request["method"] == "initialized":
            initialized = True
        elif request["method"] == "account/rateLimits/read":
            assert initialized
            mode = os.environ.get("LIMIT_MODE", "available")
            if number == int(os.environ.get("LIMIT_TIMEOUT_CALL", "0")):
                time.sleep(30)
            if mode == "unavailable":
                print(json.dumps({"id": request["id"], "error": {"message": "not available"}}), flush=True)
                continue
            window = {"usedPercent": 10 + (number - 1) * 3, "windowDurationMins": 300, "resetsAt": 2000000000}
            snapshots = json.loads(os.environ.get("LIMIT_SNAPSHOTS", "[]"))
            result = snapshots[min(number - 1, len(snapshots) - 1)] if snapshots else {
                "rateLimitsByLimitId": {"codex": {"primary": window}},
                "rateLimits": {"primary": dict(window, usedPercent=99)}}
            if mode == "no_limits":
                result = {"rateLimits": None}
            if mode == "legacy":
                result = {"rateLimits": {"primary": window}}
            print(json.dumps({"id": request["id"], "result": result}), flush=True)
    sys.exit(0)
result_path = Path(sys.argv[sys.argv.index("-o") + 1])
schema = json.loads(Path(sys.argv[sys.argv.index("--output-schema") + 1]).read_text())
is_review = "findings" in schema["properties"]
is_planning = "plan" in schema["properties"]
context_path = result_path.parent / "check-failure.json"
review_context_path = result_path.parent / "review-failure.json"
is_fix = context_path.exists() or review_context_path.exists()
prompt = sys.stdin.read()
calls_path = root / "codex-calls.jsonl"
calls = [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.exists() else []
stage = "planning" if is_planning else "review" if is_review else "review_fix" if review_context_path.exists() else "check_fix" if context_path.exists() else "coding"
call = {"fix": is_fix, "stage": stage, "prompt": prompt, "argv": sys.argv[1:], "session_id": "session-" + str(len(calls) + 1)}
for line in prompt.splitlines():
    if line.startswith("План реализации в файле: "):
        call["plan_file"] = line.split(": ", 1)[1]
        call["plan_text"] = Path(call["plan_file"]).read_text()
if is_planning:
    call["initial_content"] = Path("app/name.txt").read_text()
if context_path.exists():
    context = json.loads(context_path.read_text())
    call.update(context=context, diff=Path(context["diff_file"]).read_text(),
                logs=[Path(item["log"]).read_text() for item in context["checks"]])
if review_context_path.exists():
    context = json.loads(review_context_path.read_text())
    call.update(context=context, diff=Path(context["diff_file"]).read_text())
with calls_path.open("a") as f:
    f.write(json.dumps(call) + "\n")
print(json.dumps({"type": "thread.started", "thread_id": call["session_id"]}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
if is_planning:
    mode = os.environ.get("PLANNING_MODE", "planned")
    if mode == "fail":
        sys.exit(9)
    if mode == "partial-fail":
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 2, "cached_input_tokens": 3}}), flush=True)
        print(json.dumps({"type": "turn.failed"}), flush=True)
        sys.exit(9)
    if mode == "timeout":
        time.sleep(3)
    if mode == "interrupt":
        os.kill(os.getppid(), signal.SIGINT)
        time.sleep(3)
    if mode.startswith("mutate-source"):
        Path("app/name.txt").write_text("planner changed source\n")
    if mode == "mutate-new":
        Path("app/planner.txt").write_text("planner created source\n")
    if mode == "mutate-untracked":
        Path("app/untracked.txt").write_text("planner changed new source\n")
    if mode == "mutate-permissions":
        Path("app/name.txt").chmod(0o755)
    if mode == "mutate-index":
        subprocess.run([os.environ["REAL_GIT"], "update-index", "--chmod=+x", "app/name.txt"], check=True)
    if mode == "branch":
        subprocess.run([os.environ["REAL_GIT"], "switch", "-c", "unexpected"], check=True)
    if mode == "head":
        subprocess.run([os.environ["REAL_GIT"], "commit", "--allow-empty", "-m", "unexpected"], check=True)
    if mode == "mutate-source-fail":
        sys.exit(9)
    result = {"status": "planned", "summary": "Implementation plan", "plan": "# Test plan\nImplement and verify the feature.", "blockers": []}
    if mode in ("blocked", "needs_input"):
        result.update(status=mode, plan="", blockers=["Need planning details"])
    if mode == "empty":
        result["plan"] = ""
    if mode == "malformed":
        result = {"unexpected": True}
    result_path.write_text(json.dumps(result))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 40,
                       "output_tokens": 6, "cached_input_tokens": 20}}))
    sys.exit(0)
if is_review:
    mode = os.environ.get("REVIEW_MODE", "reviewed")
    if mode == "fail":
        sys.exit(9)
    if mode == "timeout":
        time.sleep(3)
    if mode == "interrupt":
        os.kill(os.getppid(), signal.SIGINT)
        time.sleep(3)
    if mode == "mutate-source":
        Path("app/name.txt").write_text("review changed source\n")
    if mode == "mutate-index":
        subprocess.run([os.environ["REAL_GIT"], "reset", "HEAD", "--", "app/name.txt"], check=True)
    if mode == "branch":
        subprocess.run([os.environ["REAL_GIT"], "switch", "-c", "unexpected"], check=True)
    if mode == "head":
        subprocess.run([os.environ["REAL_GIT"], "commit", "--allow-empty", "-m", "unexpected"], check=True)
    review_number = sum(c["stage"] == "review" for c in calls)
    rounds = json.loads(os.environ.get("REVIEW_ROUNDS", "[]"))
    priorities = rounds[review_number] if review_number < len(rounds) else []
    result = {"status": "reviewed", "summary": "Review", "blockers": [],
              "findings": [{"priority": p, "file": "app/name.txt", "line": 1,
                            "explanation": "Fix " + p} for p in priorities]}
    if mode in ("blocked", "needs_input"):
        result.update(status=mode, blockers=["Need more information"])
    if mode == "malformed":
        result = {"unexpected": True}
    result_path.write_text(json.dumps(result))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 25,
                       "output_tokens": 3, "cached_input_tokens": 15}}))
    sys.exit(0)
if os.environ.get("PARTIAL_USAGE") and is_fix:
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 2, "cached_input_tokens": 3}}), flush=True)
    print(json.dumps({"type": "turn.started"}), flush=True)
result = {"status": "implemented", "summary": "Original implementation" if not is_fix else "Repair summary",
          "manual_checks": ["Original manual check"] if not is_fix else ["Repair manual check"], "blockers": [],
          "verification_limits": json.loads(os.environ.get("AGENT_LIMITS", "{}")).get(stage, [])}
mode = os.environ.get("FIX_MODE", "fix") if is_fix else os.environ.get("INITIAL_MODE", "initial")
if mode == "fail":
    sys.exit(9)
if mode == "timeout":
    time.sleep(3)
if mode == "interrupt":
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(3)
if mode == "initial":
    Path("app/name.txt").write_text("fixed\n" if os.environ.get("INITIAL_GOOD") else "broken\n")
    if os.environ.get("INITIAL_WHITESPACE"):
        Path("app/name.txt").write_text("broken \n")
    if os.environ.get("INITIAL_FORBIDDEN"):
        Path("app/secret.jks").write_text("secret")
elif mode != "noop":
    Path("app/name.txt").write_text("fixed\n")
    if stage == "review_fix":
        Path("app/review.txt").write_text("review repair " + call["session_id"] + "\n")
    if stage == "check_fix" and os.environ.get("CHECK_MODE") == "review-fix-fails":
        Path("app/check-repaired.txt").write_text("checks repaired\n")
if is_fix and os.environ.get("ADD_REPAIR_FILE"):
    Path("app/new.txt").write_text("new repair file\n")
if mode in ("blocked", "needs_input"):
    result.update(status=mode, blockers=["Need SDK configuration"])
if mode == "malformed":
    result = {"wrong": True}
if mode == "contradictory":
    result["blockers"] = ["Implementation unfinished"]
if mode == "empty-blocked":
    result["status"] = "blocked"
if mode == "missing-limits":
    result.pop("verification_limits")
if mode == "bad-limits":
    result["verification_limits"] = [42]
if mode == "forbidden":
    Path("app/secret.jks").write_text("secret")
if mode == "branch":
    subprocess.run([os.environ["REAL_GIT"], "switch", "-c", "unexpected"], check=True)
if mode == "head":
    subprocess.run([os.environ["REAL_GIT"], "commit", "--allow-empty", "-m", "unexpected"], check=True)
result_path.write_text(json.dumps(result))
usage_mode = os.environ.get("USAGE_MODE")
event = {"type": "turn.completed"}
if usage_mode and usage_mode != "missing":
    event["usage"] = {"input_tokens": 50 if is_fix else 100, "output_tokens": 5 if is_fix else 20,
                      "cached_input_tokens": 30 if is_fix else 60}
if usage_mode == "damaged":
    print("not-json")
if usage_mode == "invalid_counts":
    event["usage"] = {"input_tokens": 100, "output_tokens": "wrong", "cached_input_tokens": -1}
if usage_mode == "multiple":
    print(json.dumps(event))
    print(json.dumps({"type": "turn.started"}))
print(json.dumps(event))
'''

CHECK_SCRIPT = r'''
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(__file__).resolve().parent
name = sys.argv[1]
content = Path("app/name.txt").read_text()
with (root / "check-calls.jsonl").open("a") as f:
    f.write(json.dumps({"name": name, "content": content}) + "\n")
if name in ("setup", "first"):
    sys.exit(0)
mode = os.environ.get("CHECK_MODE", "normal")
if mode.startswith("mutate-source"):
    Path("app/name.txt").write_text("changed by check\n")
    sys.exit(1 if mode.endswith("fail") else 0)
if mode == "mutate-index":
    subprocess.run([os.environ["REAL_GIT"], "reset", "HEAD", "--", "app/name.txt"], check=True)
    sys.exit(1)
if mode == "review-fix-fails" and Path("app/review.txt").exists() and not Path("app/check-repaired.txt").exists():
    print("compiler: review fix broke build", flush=True)
    sys.exit(1)
if mode == "timeout" and content != "fixed\n":
    print("compiler: unresolved BrokenSymbol", flush=True)
    time.sleep(3)
if mode == "always" or content != "fixed\n":
    print("compiler: unresolved BrokenSymbol", flush=True)
    sys.exit(1)
'''


class CheckRepairTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo, self.remote = self.root / "repo", self.root / "remote.git"
        self.repo.mkdir()
        self.env = {**os.environ, "REAL_GIT": REAL_GIT, "TEST_ROOT": str(self.root),
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.g(self.root, "init", "--bare", str(self.remote))
        self.g(self.repo, "init", "-b", "develop")
        self.g(self.repo, "config", "user.name", "Test Developer")
        self.g(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "app").mkdir()
        (self.repo / "app/name.txt").write_text("base\n")
        (self.repo / ".gitignore").write_text("local.properties\n.gradle/\n")
        self.g(self.repo, "add", ".")
        self.g(self.repo, "commit", "-m", "Initial")
        self.g(self.repo, "remote", "add", "origin", str(self.remote))
        self.g(self.repo, "push", "-u", "origin", "develop")
        binary_dir = self.root / "bin"
        binary_dir.mkdir()
        for name, body in (("git", FAKE_GIT), ("gh", FAKE_GH), ("codex", FAKE_CODEX)):
            executable = binary_dir / name
            executable.write_text("#!" + sys.executable + "\n" + textwrap.dedent(body))
            executable.chmod(0o755)
        self.env["PATH"] = str(binary_dir) + os.pathsep + os.environ["PATH"]
        self.check_script = self.root / "check.py"
        self.check_script.write_text(CHECK_SCRIPT)
        self.config_path = self.root / "config.json"
        self.cfg = {"repo": str(self.repo), "github_repo": "github.test/team/app",
                    "worktrees_root": str(self.root / "worktrees"), "runs_root": str(self.root / "runs"),
                    "checks": [self.step("first"), self.step("second")],
                    "allow_paths": ["app/**"], "deny_paths": ["**/*.jks"]}
        self.write_config()
        self.card = self.root / "task.md"
        self.card.write_text("---\nissue: APP-1\nbase: develop\ntitle: Test task\ncomplexity: trivial\n---\nImplement the requested feature.\n")

    def step(self, name):
        return {"name": name, "argv": [sys.executable, str(self.check_script), name]}

    def g(self, cwd, *args):
        result = subprocess.run([REAL_GIT, *args], cwd=cwd, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def write_config(self):
        self.config_path.write_text(json.dumps(self.cfg))

    def run_task(self, command="run", *flags, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), command, str(self.card),
                                 "--config", str(self.config_path), *flags],
                                env=self.env, text=True, capture_output=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def state_path(self):
        return next((self.root / "runs").glob("*/APP-1/state.json"))

    def state(self):
        return json.loads(self.state_path().read_text())

    def calls(self):
        path = self.root / "codex-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def checks(self):
        path = self.root / "check-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_not_published(self):
        self.assertNotIn("commit_sha", self.state())
        self.assertFalse((self.root / "github-prs.json").exists())
        self.assertFalse(self.g(self.root, "--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)", "refs/heads/feature"))

    def test_failed_build_is_repaired_and_all_checks_repeat(self):
        self.run_task("run", "--local-only")
        state = self.state()
        self.assertEqual(state["phase"], "verified")
        self.assertEqual(state["check_fix_attempts"], 1)
        self.assertFalse(state["pending_check_fix"])
        self.assertEqual([r["name"] for r in self.checks()], ["first", "second", "first", "second"])
        calls = self.calls()
        self.assertEqual([c["fix"] for c in calls], [False, True])
        self.assertNotEqual(calls[0]["session_id"], calls[1]["session_id"])
        self.assertIn("compiler: unresolved BrokenSymbol", calls[1]["logs"][-1])
        self.assertEqual(calls[1]["context"]["checks"][-1]["name"], "second")
        self.assertEqual(calls[1]["context"]["card_file"], str(self.card))
        self.assertIn("broken", calls[1]["diff"])
        self.assertIn("Implement the requested feature", calls[1]["prompt"])
        self.assertEqual(state["result"]["summary"], "Original implementation\n\nИсправления checks: Repair summary")
        self.assertEqual(state["result"]["manual_checks"], ["Original manual check", "Repair manual check"])
        history = state["check_fix_history"][0]
        self.assertEqual(history["status"], "implemented")
        self.assertEqual(history["session_id"], calls[1]["session_id"])
        self.assertGreaterEqual(history["seconds"], 0)
        self.assertTrue((Path(history["directory"]) / "fix.json").exists())
        self.assert_not_published()

    def test_default_limit_persists_across_run_commands(self):
        self.env["FIX_MODE"] = "noop"
        self.assertIn("3/3", self.run_task(success=False).stderr)
        self.assertEqual(self.state()["check_fix_attempts"], 3)
        self.assertEqual(len(self.calls()), 4)
        retry = self.run_task(success=False)
        self.assertIn("лимит", retry.stderr)
        self.assertIn("check-2.log", retry.stderr)
        self.assertEqual(len(self.calls()), 4)
        self.assert_not_published()

    def test_zero_disables_repairs(self):
        self.cfg["max_check_fix_iterations"] = 0
        self.write_config()
        self.assertIn("отключено", self.run_task(success=False).stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assert_not_published()

    def test_custom_limit(self):
        self.cfg["max_check_fix_iterations"] = 1
        self.write_config()
        self.env["FIX_MODE"] = "noop"
        self.assertIn("1/1", self.run_task(success=False).stderr)
        self.assertEqual(len(self.calls()), 2)

    def test_rerun_resets_budget_but_preserves_history(self):
        self.cfg["max_check_fix_iterations"] = 1
        self.write_config()
        self.env["FIX_MODE"] = "noop"
        self.run_task(success=False)
        self.env.pop("FIX_MODE")
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual(self.state()["check_fix_attempts"], 1)
        self.assertEqual(len(self.state()["check_fix_history"]), 2)
        self.assertEqual(len(self.calls()), 4)

    def test_failed_fix_preserves_result_and_resumes_without_setup(self):
        self.cfg["setup"] = [self.step("setup")]
        self.write_config()
        self.env["FIX_MODE"] = "fail"
        self.run_task(success=False)
        state = self.state()
        self.assertEqual(state["result"]["summary"], "Original implementation")
        self.assertTrue(state["pending_check_fix"])
        self.assertEqual(state["check_fix_attempts"], 1)
        self.assertEqual(state["check_fix_history"][0]["status"], "failed")
        self.env.pop("FIX_MODE")
        self.run_task("run", "--local-only")
        self.assertEqual(self.state()["check_fix_attempts"], 2)
        self.assertEqual(sum(c["name"] == "setup" for c in self.checks()), 1)
        self.assertEqual([c["fix"] for c in self.calls()], [False, True, True])
        self.assertNotIn("last_error", self.state())

    def test_blocked_partial_fix_cannot_be_published(self):
        self.env.update(FIX_MODE="blocked", ADD_REPAIR_FILE="1")
        self.assertIn("Need SDK configuration", self.run_task(success=False).stderr)
        self.assertEqual(self.state()["result"]["summary"], "Original implementation")
        self.assertEqual(self.state()["result"]["blockers"], [])
        self.assertEqual(self.state()["check_fix_history"][0]["result"]["status"], "blocked")
        self.assertIn("не завершено", self.run_task("publish", success=False).stderr)
        self.assertEqual(len(self.calls()), 2)
        self.env.pop("FIX_MODE")
        self.run_task("run", "--local-only")
        self.assertEqual(len(self.calls()), 3)
        self.assertIn("app/new.txt", self.calls()[-1]["diff"])
        self.assertEqual(self.state()["phase"], "verified")

    def test_needs_input_stops_and_preserves_original(self):
        self.env["FIX_MODE"] = "needs_input"
        self.run_task(success=False)
        self.assertEqual(self.state()["result"]["summary"], "Original implementation")
        self.assertTrue(self.state()["pending_check_fix"])
        self.assert_not_published()

    def test_fix_timeout_consumes_attempt_and_limit_is_not_reset(self):
        self.cfg.update(codex_timeout_seconds=1, max_check_fix_iterations=1)
        self.write_config()
        self.env["FIX_MODE"] = "timeout"
        self.run_task(success=False)
        history = self.state()["check_fix_history"][0]
        self.assertEqual(history["status"], "failed")
        self.assertEqual(history["session_id"], "session-2")
        self.assertIn("TimeoutExpired", history["error"])
        self.assertTrue(self.state()["pending_check_fix"])
        self.run_task(success=False)
        self.assertEqual(len(self.calls()), 2)
        self.assert_not_published()

    def test_interrupted_fix_is_recorded_and_can_resume(self):
        self.env["FIX_MODE"] = "interrupt"
        result = self.run_task(success=False)
        self.assertEqual(result.returncode, 130)
        self.assertEqual(self.state()["check_fix_history"][0]["status"], "interrupted")
        self.assertEqual(self.state()["result"]["summary"], "Original implementation")
        self.env.pop("FIX_MODE")
        self.run_task("run", "--local-only")
        self.assertEqual(self.state()["check_fix_attempts"], 2)

    def test_malformed_fix_result_stops(self):
        self.env["FIX_MODE"] = "malformed"
        self.run_task(success=False)
        self.assertEqual(self.state()["result"]["summary"], "Original implementation")
        self.assertTrue(self.state()["pending_check_fix"])
        self.assert_not_published()

    def test_check_timeout_is_passed_to_repair_session(self):
        self.cfg["checks"][1]["timeout_seconds"] = 1
        self.write_config()
        self.env["CHECK_MODE"] = "timeout"
        self.run_task("run", "--local-only")
        self.assertEqual(self.calls()[1]["context"]["checks"][-1]["exit_code"], 124)
        self.assertEqual(self.state()["phase"], "verified")

    def test_whitespace_errors_are_repaired_before_build_checks(self):
        self.env["INITIAL_WHITESPACE"] = "1"
        self.run_task("run", "--local-only")
        context = self.calls()[1]["context"]
        self.assertEqual(context["checks"][0]["name"], "git diff --check")
        self.assertIn("trailing whitespace", self.calls()[1]["logs"][0])
        self.assertEqual(len(self.checks()), 2)

    def test_setup_failure_does_not_launch_repair(self):
        self.cfg["setup"] = [{"name": "fail setup", "argv": [sys.executable, "-c", "raise SystemExit(7)"]}]
        self.write_config()
        self.run_task(success=False)
        self.assertFalse(self.calls())
        self.assertFalse(self.state().get("check_fix_history"))

    def test_check_that_changes_source_is_not_repaired(self):
        self.env["CHECK_MODE"] = "mutate-source"
        self.assertIn("Проверки изменили", self.run_task(success=False).stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assert_not_published()

    def test_failed_check_that_changes_source_is_not_repaired(self):
        self.env["CHECK_MODE"] = "mutate-source-fail"
        self.assertIn("Проверки изменили", self.run_task(success=False).stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assert_not_published()

    def test_failed_check_that_changes_index_is_not_repaired(self):
        self.env["CHECK_MODE"] = "mutate-index"
        self.assertIn("Проверки изменили", self.run_task(success=False).stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_forbidden_initial_change_does_not_launch_repair(self):
        self.env["INITIAL_FORBIDDEN"] = "1"
        self.assertIn("deny_paths", self.run_task(success=False).stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_forbidden_fix_stops_and_cannot_launch_another_session(self):
        self.env["FIX_MODE"] = "forbidden"
        self.assertIn("deny_paths", self.run_task(success=False).stderr)
        self.assertEqual(self.state()["result"]["summary"], "Original implementation")
        self.run_task(success=False)
        self.assertEqual(len(self.calls()), 2)
        self.assert_not_published()

    def test_branch_change_by_fix_stops_workflow(self):
        self.env["FIX_MODE"] = "branch"
        self.assertIn("изменилась рабочая ветка", self.run_task(success=False).stderr)
        self.assertEqual(self.state()["check_fix_history"][0]["status"], "failed")
        self.run_task(success=False)
        self.assertEqual(len(self.calls()), 2)

    def test_head_change_by_fix_stops_workflow(self):
        self.env["FIX_MODE"] = "head"
        self.assertIn("HEAD изменился", self.run_task(success=False).stderr)
        self.run_task(success=False)
        self.assertEqual(len(self.calls()), 2)

    def test_publish_does_not_repair_and_invalidates_previous_check_result(self):
        self.env["INITIAL_GOOD"] = "1"
        self.run_task("run", "--local-only")
        self.assertIn("checked_tree", self.state())
        self.env["CHECK_MODE"] = "always"
        self.assertIn("через run", self.run_task("publish", success=False).stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertNotIn("checked_tree", self.state())
        self.assertNotIn("checks", self.state())
        self.assert_not_published()

    def test_committed_task_cannot_be_repaired(self):
        self.env.update(INITIAL_GOOD="1", GH_LOST_RESPONSE="1")
        self.run_task(success=False)
        self.assertIn("commit_sha", self.state())
        self.env["CHECK_MODE"] = "always"
        self.assertIn("После коммита", self.run_task(success=False).stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_full_run_publishes_after_repair_and_does_not_duplicate(self):
        self.run_task()
        state = self.state()
        self.assertEqual(state["phase"], "published")
        self.assertEqual(self.g(self.root, "--git-dir", str(self.remote), "rev-parse", "feature/APP-1"), state["commit_sha"])
        self.run_task()
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(len(json.loads((self.root / "github-prs.json").read_text())), 1)

    def test_successful_checks_do_not_launch_repairs(self):
        self.env["INITIAL_GOOD"] = "1"
        self.run_task("run", "--local-only")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.state()["phase"], "verified")

    def test_existing_implemented_task_needs_no_rerun(self):
        self.env["INITIAL_GOOD"] = "1"
        self.run_task("run", "--local-only")
        state = self.state()
        state["phase"] = "implemented"
        for key in ("check_fix_attempts", "check_fix_history", "pending_check_fix", "last_check_failure"):
            state.pop(key, None)
        self.state_path().write_text(json.dumps(state))
        (Path(state["worktree"]) / "app/name.txt").write_text("broken\n")
        self.run_task("run", "--local-only")
        self.assertEqual([c["fix"] for c in self.calls()], [False, True])
        self.assertEqual(self.state()["check_fix_attempts"], 1)

    def test_manual_fix_can_pass_checks_after_exhausting_budget(self):
        self.cfg["max_check_fix_iterations"] = 0
        self.write_config()
        self.run_task(success=False)
        (Path(self.state()["worktree"]) / "app/name.txt").write_text("fixed\n")
        self.run_task("run", "--local-only")
        self.assertNotIn("last_check_failure", self.state())
        self.assertNotIn("last_error", self.state())
        self.assertEqual(len(self.calls()), 1)

    def test_invalid_limits_fail_before_codex(self):
        for value in (-1, True, 1.5, "3"):
            with self.subTest(value=value):
                self.cfg["max_check_fix_iterations"] = value
                self.write_config()
                self.assertIn("max_check_fix_iterations", self.run_task(success=False).stderr)
        self.assertFalse(self.calls())

    def test_extra_codex_args_are_preserved_for_repair(self):
        self.cfg["codex_args"] = ["--add-dir", str(self.root / "cache")]
        self.write_config()
        self.run_task("run", "--local-only")
        for call in self.calls():
            self.assertIn("--add-dir", call["argv"])
            self.assertIn(str(self.root / "cache"), call["argv"])
            self.assertEqual(call["argv"][call["argv"].index("--sandbox") + 1], "workspace-write")


if __name__ == "__main__":
    unittest.main()
