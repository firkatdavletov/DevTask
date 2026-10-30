"""Agent verification limits never replace the controller's mandatory checks."""
import json
import sys
import unittest

from dev_task.state import migrate_state

import test_check_repairs as repairs
from test_commit_messages import COMMIT_CODEX


class LegacyResultTest(unittest.TestCase):
    def test_missing_limits_do_not_reclassify_saved_blockers(self):
        state = {"base_sha": "base", "result": {"status": "implemented",
                 "blockers": ["SocketException: Operation not permitted"]}}
        migrate_state(state)
        self.assertEqual(state["result"]["verification_limits"], [])
        self.assertEqual(state["result"]["blockers"], ["SocketException: Operation not permitted"])


class VerificationLimitsTest(unittest.TestCase):
    g = repairs.CheckRepairTest.g
    step = repairs.CheckRepairTest.step
    write_config = repairs.CheckRepairTest.write_config
    run_task = repairs.CheckRepairTest.run_task
    state = repairs.CheckRepairTest.state
    calls = repairs.CheckRepairTest.calls
    checks = repairs.CheckRepairTest.checks
    assert_not_published = repairs.CheckRepairTest.assert_not_published

    def setUp(self):
        repairs.CheckRepairTest.setUp(self)
        self.card.write_text(self.card.read_text().replace("trivial", "simple"))
        (self.root / "bin/codex").write_text("#!" + sys.executable + "\n" + COMMIT_CODEX + repairs.FAKE_CODEX)
        self.env["INITIAL_GOOD"] = "1"
        self.limit = "Gradle: FileLockContentionHandler SocketException: Operation not permitted"
        self.env["AGENT_LIMITS"] = json.dumps({"coding": [self.limit],
                                              "check_fix": [self.limit, "No ADB socket"],
                                              "review_fix": [self.limit, "No device"]})

    def state_path(self):
        return next((self.root / "runs").glob("*/KRMA-1/state.json"))

    def test_limits_allow_checks_review_and_publish_and_are_reported(self):
        self.run_task()
        state = self.state()
        self.assertEqual(state["phase"], "published")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review"])
        self.assertEqual(len(self.checks()), 2)
        self.assertEqual(state["result"]["verification_limits"], [self.limit])
        directory = self.state_path().parent
        self.assertEqual(json.loads((directory / "report.json").read_text())["verification_limits"], [self.limit])
        for name in ("report.md", "pr-body.md"):
            text = (directory / name).read_text()
            self.assertIn("Ограничения проверок агента", text)
            self.assertIn(self.limit, text)
        self.assertIn("сама по себе не является блокером review", self.calls()[1]["prompt"])

    def test_check_and_review_fixes_merge_limits_and_repeat_checks(self):
        self.env.pop("INITIAL_GOOD")
        self.env["REVIEW_ROUNDS"] = json.dumps([["P1"], []])
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()],
                         ["coding", "check_fix", "review", "review_fix", "review"])
        self.assertEqual(len(self.checks()), 6)
        self.assertEqual(self.state()["result"]["verification_limits"],
                         [self.limit, "No ADB socket", "No device"])
        for call in self.calls():
            if call["stage"] in ("coding", "check_fix", "review_fix"):
                self.assertIn("verification_limits", call["prompt"])

    def test_failed_controller_checks_prevent_review_and_publish(self):
        self.cfg["max_check_fix_iterations"] = 0
        self.write_config()
        self.env["CHECK_MODE"] = "always"
        self.run_task(success=False)
        self.assertEqual([c["stage"] for c in self.calls()], ["coding"])
        self.assertEqual(len(self.checks()), 2)
        self.assertNotIn("checked_tree", self.state())
        self.assert_not_published()
        self.env.pop("CHECK_MODE")
        self.run_task("run", "--local-only")
        self.assertEqual([c["stage"] for c in self.calls()], ["coding", "review"])

    def test_bad_or_blocked_coding_results_stop_and_normal_run_retries(self):
        for mode in ("blocked", "needs_input", "contradictory", "empty-blocked", "missing-limits", "bad-limits"):
            with self.subTest(mode=mode):
                self.env["INITIAL_MODE"] = mode
                before = len(self.checks())
                self.run_task("run", "--local-only", "--rerun", success=False)
                self.assertEqual(len(self.checks()), before)
                worktree = self.state()["worktree"]
                self.assertEqual(self.state()["phase"], "prepared")
                self.assert_not_published()
                self.env.pop("INITIAL_MODE")
                self.run_task("run", "--local-only")
                self.assertEqual(self.state()["worktree"], worktree)
                self.assertEqual(len(self.checks()), before + 2)

    def test_invalid_fix_results_do_not_reach_review_or_publish(self):
        for stage in ("check_fix", "review_fix"):
            for mode in ("blocked", "needs_input", "contradictory", "empty-blocked", "missing-limits", "bad-limits"):
                with self.subTest(stage=stage, mode=mode):
                    if stage == "check_fix":
                        self.env.pop("INITIAL_GOOD", None)
                        self.env["REVIEW_ROUNDS"] = "[]"
                    else:
                        self.env["INITIAL_GOOD"] = "1"
                        # Fake review rounds count sessions across reruns.
                        self.env["REVIEW_ROUNDS"] = json.dumps([["P1"]] * 30)
                    self.env["FIX_MODE"] = mode
                    prior = len(self.calls())
                    self.run_task("run", "--local-only", "--rerun", success=False)
                    self.assertEqual(self.calls()[-1]["stage"], stage)
                    self.assertEqual(len(self.calls()) - prior, 2 if stage == "check_fix" else 3)
                    self.assert_not_published()

    def test_legacy_saved_result_defaults_limits_without_reclassifying_blockers(self):
        self.run_task("run", "--local-only")
        state = self.state()
        state["result"].pop("verification_limits")
        self.state_path().write_text(json.dumps(state))
        count = len(self.calls())
        self.run_task("run", "--local-only")
        self.assertEqual(self.state()["result"]["verification_limits"], [])
        self.assertEqual(len(self.calls()), count)
