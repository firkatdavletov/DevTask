"""Coding, checks, review and repair integration with local Git and fake Codex/gh."""
import json
from pathlib import Path
import unittest

import test_check_repairs as repairs


class ReviewWorkflowTest(unittest.TestCase):
    g = repairs.CheckRepairTest.g
    step = repairs.CheckRepairTest.step
    write_config = repairs.CheckRepairTest.write_config
    run_task = repairs.CheckRepairTest.run_task
    state_path = repairs.CheckRepairTest.state_path
    state = repairs.CheckRepairTest.state
    calls = repairs.CheckRepairTest.calls
    checks = repairs.CheckRepairTest.checks

    def setUp(self):
        repairs.CheckRepairTest.setUp(self)
        self.complexity("simple")
        self.env["INITIAL_GOOD"] = "1"
        self.env["USAGE_MODE"] = "normal"

    def complexity(self, value):
        self.card.write_text("---\nissue: KRMA-1\nbase: develop\ntitle: Test task\ncomplexity: "
                             + value + "\n---\nImplement the requested feature.\n")

    def report(self):
        return json.loads((self.state_path().parent / "report.json").read_text())

    def stages(self):
        return [call["stage"] for call in self.calls()]

    def test_all_complexities_choose_models_and_review_skip(self):
        specs = [("trivial", "gpt-6-luna", "low", None, None),
                 ("simple", "gpt-6-luna", "medium", "gpt-6.1-sol", "low"),
                 ("medium", "gpt-6.1-sol", "medium", "gpt-6-astra", "low"),
                 ("complex", "gpt-6.1-sol", "high", "gpt-6-astra", "medium")]
        for index, (complexity, coding_model, coding_effort, review_model, review_effort) in enumerate(specs):
            self.complexity(complexity.upper())
            prior = len(self.calls())
            self.run_task("run", "--local-only", *(["--rerun"] if index else []))
            calls = [call for call in self.calls()[prior:] if call["stage"] != "planning"]
            self.assertEqual([c["stage"] for c in calls], ["coding"] + (["review"] if review_model else []))
            expected = [(calls[0], coding_model, coding_effort, "workspace-write")]
            if review_model:
                expected.append((calls[1], review_model, review_effort, "read-only"))
            for call, model, effort, sandbox in expected:
                argv = call["argv"]
                self.assertEqual(argv[argv.index("--model") + 1], model)
                self.assertEqual(argv[argv.index("-c") + 1], "model_reasoning_effort=" + effort)
                self.assertEqual(argv[argv.index("--sandbox") + 1], sandbox)
            self.assertEqual(self.state()["execution_profile"]["complexity"], complexity)
            if review_model is None:
                self.assertIn("Качество не оценивалось", (self.state_path().parent / "report.md").read_text())

    def test_review_is_readonly_and_does_not_receive_extra_write_dir(self):
        self.cfg["codex_args"] = ["--add-dir", "/tmp/gradle-cache"]
        self.write_config()
        self.run_task("run", "--local-only")
        self.assertIn("--add-dir", self.calls()[0]["argv"])
        self.assertNotIn("--add-dir", self.calls()[1]["argv"])
        self.assertEqual(self.calls()[1]["argv"][self.calls()[1]["argv"].index("--sandbox") + 1], "read-only")
        self.assertIn("checked.diff", self.calls()[1]["prompt"])
        context = json.loads(next(self.state_path().parent.glob("attempt-*/review-*/review-context.json")).read_text())
        self.assertEqual(len(context["checks"]), 2)
        self.assertIn("diff --", Path(context["diff_file"]).read_text())

    def test_review_fix_rechecks_then_reviews_again(self):
        self.env["REVIEW_ROUNDS"] = json.dumps([["P0", "P1", "P2"], []])
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["coding", "review", "review_fix", "review"])
        self.assertEqual(len(self.checks()), 4)
        self.assertEqual(self.state()["review_fix_attempts"], 1)
        self.assertEqual(self.state()["pending_review_fix"], False)
        self.assertEqual(self.report()["review_fix_started"], 1)
        self.assertEqual(self.report()["review_fix_completed"], 1)
        self.assertEqual(self.report()["quality"][0]["first_p1_p2"], 2)
        self.assertEqual(self.report()["quality"][0]["last_p1_p2"], 0)
        self.assertEqual(self.report()["stages"]["review"]["tokens"]["total_tokens"], 56)
        self.assertEqual(self.report()["stages"]["review_fix"]["tokens"]["total_tokens"], 55)
        self.assertTrue(next(self.state_path().parent.glob("attempt-*/review-fix-*/review-failure.json")).exists())

    def test_p3_is_recorded_without_repair(self):
        self.env["REVIEW_ROUNDS"] = json.dumps([["P3"]])
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["coding", "review"])
        self.assertEqual(self.report()["quality"][0]["reviews"][0]["result"]["findings"][0]["priority"], "P3")

    def test_review_limit_zero_blocks_publication(self):
        self.cfg["max_fix_iterations"] = 0
        self.write_config()
        self.env["REVIEW_ROUNDS"] = json.dumps([["P1"]])
        result = self.run_task(success=False)
        self.assertIn("лимит исправлений review", result.stderr)
        self.assertEqual(self.stages(), ["coding", "review"])
        self.assertFalse(self.state().get("commit_sha"))

    def test_three_review_repairs_are_the_limit(self):
        self.env["REVIEW_ROUNDS"] = json.dumps([["P2"], ["P2"], ["P2"], ["P2"]])
        result = self.run_task("run", "--local-only", success=False)
        self.assertIn("(3/3)", result.stderr)
        self.assertEqual(self.stages(), ["coding", "review", "review_fix", "review",
                                         "review_fix", "review", "review_fix", "review"])
        self.assertEqual(self.state()["review_fix_attempts"], 3)
        self.assertEqual(len(self.report()["quality"][0]["reviews"]), 4)

    def test_check_repair_after_review_fix_has_independent_limit(self):
        self.cfg["agents"] = {"coding": {"model": "repair-model", "effort": "high"}}
        self.write_config()
        self.env["REVIEW_ROUNDS"] = json.dumps([["P1"], []])
        self.env["CHECK_MODE"] = "review-fix-fails"
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["coding", "review", "review_fix", "check_fix", "review"])
        for call in self.calls():
            if call["stage"] in ("coding", "review_fix", "check_fix"):
                argv = call["argv"]
                self.assertEqual(argv[argv.index("--model") + 1], "repair-model")
                self.assertIn("model_reasoning_effort=high", argv)
        self.assertEqual(self.state()["check_fix_attempts"], 1)
        self.assertEqual(self.state()["review_fix_attempts"], 1)
        self.assertEqual(self.report()["stages"]["check_fix"]["session_count"], 1)

    def test_review_blocker_and_bad_result_stop(self):
        for mode in ("blocked", "malformed"):
            self.env["REVIEW_MODE"] = mode
            result = self.run_task("run", "--local-only", success=False)
            self.assertIn("review", result.stderr.lower())
            self.assertEqual(self.stages().count("coding"), 1)
            self.assertFalse(self.state().get("reviewed_tree"))
        self.env.pop("REVIEW_MODE")
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages().count("coding"), 1)
        self.assertEqual(self.stages().count("review"), 3)

    def test_review_timeout_and_interruption_resume(self):
        self.cfg["codex_timeout_seconds"] = 1
        self.write_config()
        self.env["REVIEW_MODE"] = "timeout"
        self.run_task("run", "--local-only", success=False)
        self.assertEqual(self.report()["sessions"][-1]["status"], "timed_out")
        self.env["REVIEW_MODE"] = "interrupt"
        self.assertEqual(self.run_task("run", "--local-only", success=False).returncode, 130)
        self.assertEqual(self.report()["sessions"][-1]["status"], "interrupted")
        self.env.pop("REVIEW_MODE")
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["coding", "review", "review", "review"])

    def test_readonly_review_mutations_stop_workflow(self):
        for mode in ("mutate-source", "mutate-index", "branch", "head"):
            with self.subTest(mode=mode):
                # Each mutation needs a fresh isolated worktree fixture.
                if mode != "mutate-source":
                    self.tearDownFixture()
                    repairs.CheckRepairTest.setUp(self)
                    self.complexity("simple")
                    self.env.update(INITIAL_GOOD="1", USAGE_MODE="normal")
                self.env["REVIEW_MODE"] = mode
                self.run_task("run", "--local-only", success=False)
                self.assertFalse(self.state().get("reviewed_tree"))

    def tearDownFixture(self):
        self.temp.cleanup()

    def test_failed_review_fix_preserves_result_and_budget(self):
        self.env.update(REVIEW_ROUNDS=json.dumps([["P1"], []]), FIX_MODE="fail")
        self.run_task("run", "--local-only", success=False)
        self.assertTrue(self.state()["pending_review_fix"])
        self.assertEqual(self.state()["result"]["summary"], "Original implementation")
        self.assertEqual(self.state()["review_fix_attempts"], 1)
        self.env.pop("FIX_MODE")
        self.run_task("run", "--local-only")
        self.assertEqual(self.state()["review_fix_attempts"], 2)
        self.assertEqual(self.stages().count("coding"), 1)

    def test_manual_change_blocks_publish_until_new_review(self):
        self.run_task("run", "--local-only")
        Path(self.state()["worktree"], "app", "extra.txt").write_text("manual improvement\n")
        result = self.run_task("publish", success=False)
        self.assertIn("review", result.stderr.lower())
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["coding", "review", "review"])

    def test_skip_review_persists_and_requires_rerun_to_change(self):
        self.run_task("run", "--local-only", "--skip-review")
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["coding"])
        self.assertIn("--skip-review", self.report()["review_skip_reason"])
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual(self.stages(), ["coding", "coding", "review"])
        self.assertEqual(len(self.report()["sessions"]), 3)

    def test_profile_change_requires_rerun(self):
        self.run_task("run", "--local-only")
        self.cfg["complexity_profiles"] = {"simple": {"review": {"model": "astra", "effort": "medium"}}}
        self.write_config()
        self.assertIn("--rerun", self.run_task("run", "--local-only", success=False).stderr)
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual(self.calls()[-1]["argv"][self.calls()[-1]["argv"].index("--model") + 1], "gpt-6-astra")

    def test_missing_complexity_and_conflicting_codex_args_rejected(self):
        self.card.write_text(self.card.read_text().replace("complexity: simple\n", ""))
        result = self.run_task("run", "--local-only", success=False)
        self.assertIn("complexity", result.stderr)
        self.assertEqual(self.calls(), [])
        self.complexity("simple")
        self.cfg["codex_args"] = ["-c", "model_reasoning_effort=high"]
        self.write_config()
        result = self.run_task("run", "--local-only", success=False)
        self.assertIn("codex_args", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_old_uncommitted_state_gets_review_but_old_commit_is_stopped(self):
        self.run_task("run", "--local-only")
        state = self.state()
        for name in ("execution_profile", "workflow_version", "cycle_id", "review_skip_reason",
                     "reviewed_tree", "review_tree", "review_result"):
            state.pop(name, None)
        self.state_path().write_text(json.dumps(state))
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["coding", "review", "review"])
        state = self.state()
        state.pop("execution_profile")
        state["commit_sha"] = state["base_sha"]
        state["phase"] = "committed"
        self.state_path().write_text(json.dumps(state))
        self.assertIn("вручную", self.run_task("publish", success=False).stderr)

    def test_published_repeat_does_not_duplicate_review(self):
        self.run_task("run")
        self.run_task("run")
        self.assertEqual(self.stages(), ["coding", "review"])
        self.assertEqual(self.report()["stages"]["review"]["session_count"], 1)


if __name__ == "__main__":
    unittest.main()
