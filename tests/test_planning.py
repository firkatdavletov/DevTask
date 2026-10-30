"""Planning sessions, read-only guards, resume and migration with local fakes."""
import hashlib
import json
from pathlib import Path
import unittest

import test_check_repairs as repairs


class PlanningWorkflowTest(unittest.TestCase):
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
        self.complexity("medium")
        self.env.update(INITIAL_GOOD="1", USAGE_MODE="normal")

    def complexity(self, value):
        self.card.write_text("---\nissue: APP-1\nbase: develop\ntitle: Test task\ncomplexity: "
                             + value + "\n---\nImplement the requested feature.\n")

    def stages(self):
        return [item["stage"] for item in self.calls()]

    def report(self):
        return json.loads((self.state_path().parent / "report.json").read_text())

    def as_v2(self, **changes):
        state = self.state()
        state["workflow_version"] = 2
        state["execution_profile"].pop("planning", None)
        for field in ("planning_status", "planning_skip_reason", "planning_result", "plan_path", "plan_sha256",
                      "setup_completed", "coding_started"):
            state.pop(field, None)
        state.update(changes)
        self.state_path().write_text(json.dumps(state))

    def test_all_complexities_and_planning_models(self):
        for index, complexity in enumerate(("trivial", "simple", "medium", "complex")):
            self.complexity(complexity)
            before = len(self.calls())
            self.run_task("run", "--local-only", *(["--rerun"] if index else []))
            calls = self.calls()[before:]
            if complexity in ("trivial", "simple"):
                self.assertFalse(any(call["stage"] == "planning" for call in calls))
                self.assertEqual(self.state()["planning_status"], "skipped")
            else:
                self.assertEqual([call["stage"] for call in calls], ["planning", "coding", "review"])
                argv = calls[0]["argv"]
                self.assertEqual(argv[argv.index("--model") + 1], "gpt-6.1-sol" if complexity == "medium" else "gpt-6-astra")
                self.assertEqual(argv[argv.index("-c") + 1], "model_reasoning_effort=medium" if complexity == "medium" else "model_reasoning_effort=high")
                self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
                path = Path(self.state()["plan_path"])
                self.assertFalse(path.is_relative_to(Path(self.state()["worktree"])))
                self.assertEqual(self.state()["plan_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertTrue(all(call["plan_file"] == str(path) for call in calls[1:]))
                self.assertEqual(len({call["session_id"] for call in calls}), 3)

    def test_profile_overrides_null_and_readonly_arguments(self):
        self.cfg.update(models={"planner": "custom-planner"}, codex_args=["--add-dir", "/tmp/gradle-cache"],
                        complexity_profiles={"medium": {"planning": {"model": "planner", "effort": "high"}}})
        self.write_config()
        self.run_task("run", "--local-only")
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "custom-planner")
        self.assertNotIn("--add-dir", argv)
        self.assertIn("--add-dir", self.calls()[1]["argv"])
        self.cfg["complexity_profiles"]["medium"]["planning"] = None
        self.write_config()
        self.assertIn("--rerun", self.run_task("run", "--local-only", success=False).stderr)
        before = len(self.calls())
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual([c["stage"] for c in self.calls()[before:]], ["coding", "review"])
        self.assertNotIn("plan_file", self.calls()[before])

    def test_skip_planning_is_persisted_and_rerun_reenables_it(self):
        self.run_task("run", "--local-only", "--skip-planning")
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["coding", "review"])
        self.assertEqual(self.state()["planning_skip_reason"], "--skip-planning")
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual(self.stages(), ["coding", "review", "planning", "coding", "review"])
        self.assertIn("--rerun", self.run_task("run", "--local-only", "--skip-planning", success=False).stderr)

    def test_setup_is_not_repeated_after_planning_failure(self):
        self.cfg["setup"] = [self.step("setup")]
        self.write_config()
        self.env["PLANNING_MODE"] = "fail"
        self.run_task("run", "--local-only", success=False)
        self.assertTrue(self.state()["setup_completed"])
        self.assertEqual(self.stages(), ["planning"])
        self.env.pop("PLANNING_MODE")
        self.run_task("run", "--local-only")
        self.assertEqual(sum(item["name"] == "setup" for item in self.checks()), 1)
        self.assertEqual(self.stages(), ["planning", "planning", "coding", "review"])

    def test_coding_failure_keeps_plan_and_setup(self):
        self.cfg["setup"] = [self.step("setup")]
        self.write_config()
        self.env["INITIAL_MODE"] = "fail"
        self.run_task("run", "--local-only", success=False)
        plan = self.state()["plan_path"]
        self.assertTrue(self.state()["coding_started"])
        self.env.pop("INITIAL_MODE")
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["planning", "coding", "coding", "review"])
        self.assertEqual(self.state()["plan_path"], plan)
        self.assertEqual(sum(item["name"] == "setup" for item in self.checks()), 1)

    def test_blockers_bad_results_and_errors_stop_before_coding(self):
        for mode in ("blocked", "needs_input", "malformed", "empty", "fail"):
            self.env["PLANNING_MODE"] = mode
            self.run_task("run", "--local-only", success=False)
            self.assertEqual(self.state()["planning_status"], "pending")
            self.assertFalse(self.state()["coding_started"])
            self.assertNotIn("coding", self.stages())
            before = len(self.calls())
            self.run_task("publish", success=False)
            self.assertEqual(len(self.calls()), before)
        self.assertEqual(len(self.state()["planning_history"]), 5)
        self.env.pop("PLANNING_MODE")
        self.run_task("run", "--local-only")

    def test_timeout_and_interrupt_are_saved_and_resume(self):
        self.cfg["codex_timeout_seconds"] = 1
        self.write_config()
        self.env["PLANNING_MODE"] = "timeout"
        self.run_task("run", "--local-only", success=False)
        self.assertEqual(self.state()["planning_history"][-1]["status"], "timed_out")
        self.env["PLANNING_MODE"] = "interrupt"
        self.assertEqual(self.run_task("run", "--local-only", success=False).returncode, 130)
        self.assertEqual(self.state()["planning_history"][-1]["status"], "interrupted")
        self.env.pop("PLANNING_MODE")
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["planning", "planning", "planning", "coding", "review"])

    def test_planner_mutations_are_detected_even_on_process_failure(self):
        modes = ("mutate-source", "mutate-new", "mutate-permissions", "mutate-index", "branch", "head", "mutate-source-fail")
        for index, mode in enumerate(modes):
            with self.subTest(mode=mode):
                if index:
                    self.temp.cleanup()
                    repairs.CheckRepairTest.setUp(self)
                    self.complexity("medium")
                    self.env.update(INITIAL_GOOD="1", USAGE_MODE="normal")
                self.env["PLANNING_MODE"] = mode
                self.run_task("run", "--local-only", success=False)
                self.assertEqual(self.stages(), ["planning"])
                self.assertNotIn("plan_path", self.state())
                self.assertEqual(self.state()["planning_status"], "pending")
                self.assertEqual(self.report()["sessions"][0]["status"], "failed")

    def test_dirty_rerun_preserves_index_content_and_new_files(self):
        self.run_task("run", "--local-only")
        worktree = Path(self.state()["worktree"])
        (worktree / "app/name.txt").write_text("staged improvement\n")
        self.g(worktree, "add", "app/name.txt")
        (worktree / "app/name.txt").write_text("unstaged improvement\n")
        (worktree / "app/untracked.txt").write_text("new content\n")
        before = self.g(worktree, "diff", "--cached")
        self.env["INITIAL_MODE"] = "fail"
        self.run_task("run", "--local-only", "--rerun", success=False)
        self.assertEqual(self.g(worktree, "diff", "--cached"), before)
        self.assertEqual((worktree / "app/name.txt").read_text(), "unstaged improvement\n")
        self.assertEqual((worktree / "app/untracked.txt").read_text(), "new content\n")
        self.assertEqual([c for c in self.calls() if c["stage"] == "planning"][-1]["initial_content"], "unstaged improvement\n")

    def test_changes_to_existing_untracked_files_are_detected(self):
        self.run_task("run", "--local-only")
        worktree = Path(self.state()["worktree"])
        (worktree / "app/untracked.txt").write_text("keep this\n")
        self.env["PLANNING_MODE"] = "mutate-untracked"
        result = self.run_task("run", "--local-only", "--rerun", success=False)
        self.assertIn("Planning изменил", result.stderr)

    def test_missing_or_modified_plan_requires_rerun(self):
        self.run_task("run", "--local-only")
        plan = Path(self.state()["plan_path"])
        before = len(self.calls())
        plan.unlink()
        self.assertIn("--rerun", self.run_task("run", "--local-only", success=False).stderr)
        plan.write_text("modified plan")
        self.assertIn("--rerun", self.run_task("publish", success=False).stderr)
        self.assertEqual(len(self.calls()), before)
        self.run_task("report")
        self.run_task("run", "--local-only", "--rerun")
        self.assertNotEqual(self.state()["plan_path"], str(plan))
        self.assertEqual(self.stages().count("planning"), 2)

    def test_plan_is_passed_to_checks_and_review_repairs_without_replanning(self):
        self.env.pop("INITIAL_GOOD")
        self.env["REVIEW_ROUNDS"] = json.dumps([["P1"], []])
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["planning", "coding", "check_fix", "review", "review_fix", "review"])
        self.assertTrue(all(call.get("plan_file") == self.state()["plan_path"] for call in self.calls()[1:]))
        report = self.report()
        self.assertEqual(report["stages"]["planning"]["tokens"]["total_tokens"], 46)
        self.assertEqual(report["total"]["tokens"]["total_tokens"], 332)
        self.assertEqual(report["sessions"][0]["stage"], "planning")
        self.assertEqual(report["total"]["limit_usage"][0]["known_percentage_points"], 18)
        self.run_task("run", "--local-only")
        self.run_task("report")
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 332)

    def test_partial_planning_usage_is_preserved(self):
        self.env["PLANNING_MODE"] = "partial-fail"
        self.run_task("run", "--local-only", success=False)
        self.assertEqual(self.report()["stages"]["planning"]["tokens"]["total_tokens"], 9)
        self.assertEqual(self.report()["total"]["tokens"]["status"], "partial")

    def test_v2_completed_coding_migrates_without_replanning_or_resetting_limits(self):
        self.run_task("run", "--local-only")
        accepted = self.state()["reviewed_tree"]
        self.as_v2(review_fix_attempts=2, check_fix_attempts=1)
        calls = len(self.calls())
        self.run_task("run", "--local-only")
        self.assertEqual(len(self.calls()), calls)
        self.assertEqual(self.state()["workflow_version"], 3)
        self.assertEqual(self.state()["reviewed_tree"], accepted)
        self.assertEqual(self.state()["review_fix_attempts"], 2)
        self.assertEqual(self.state()["check_fix_attempts"], 1)
        self.assertEqual(self.state()["planning_skip_reason"], "цикл начат до добавления planning")
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual(self.stages().count("planning"), 2)
        self.assertEqual(self.state()["review_fix_attempts"], 0)
        self.assertEqual(self.state()["check_fix_attempts"], 0)

    def test_v2_failed_coding_migrates_using_current_cycle_logs(self):
        self.env["INITIAL_MODE"] = "fail"
        self.run_task("run", "--local-only", success=False)
        self.as_v2()
        self.env.pop("INITIAL_MODE")
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["planning", "coding", "coding", "review"])
        self.assertEqual(self.state()["planning_status"], "skipped")

    def test_v2_prepared_without_coding_runs_planning(self):
        self.cfg["setup"] = [{"name": "bad setup", "argv": ["false"]}]
        self.write_config()
        self.run_task("run", "--local-only", success=False)
        self.as_v2()
        self.cfg["setup"] = [self.step("setup")]
        self.write_config()
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["planning", "coding", "review"])

    def test_v2_ambiguous_coding_log_keeps_legacy_cycle(self):
        self.env["INITIAL_MODE"] = "fail"
        self.run_task("run", "--local-only", success=False)
        self.as_v2()
        (Path(self.state()["last_attempt"]) / "usage.json").unlink()
        self.env.pop("INITIAL_MODE")
        self.run_task("run", "--local-only")
        self.assertEqual(self.stages(), ["planning", "coding", "coding", "review"])
        self.assertEqual(self.state()["planning_skip_reason"], "цикл начат до добавления planning")

    def test_v2_committed_review_can_publish_and_published_task_is_unchanged(self):
        self.run_task("run")
        self.as_v2(phase="committed")
        before = len(self.calls())
        self.run_task("publish")
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(self.state()["phase"], "published")
        self.assertEqual(self.state()["planning_status"], "skipped")
        self.run_task("run")
        self.assertEqual(len(self.calls()), before)


if __name__ == "__main__":
    unittest.main()
