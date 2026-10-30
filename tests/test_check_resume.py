"""Resume a failed repair after correcting the external build environment."""
import unittest

import test_check_repairs as repairs


class CheckResumeTest(unittest.TestCase):
    g = repairs.CheckRepairTest.g
    step = repairs.CheckRepairTest.step
    write_config = repairs.CheckRepairTest.write_config
    run_task = repairs.CheckRepairTest.run_task
    state = repairs.CheckRepairTest.state
    calls = repairs.CheckRepairTest.calls
    checks = repairs.CheckRepairTest.checks

    def setUp(self):
        repairs.CheckRepairTest.setUp(self)
        self.cfg["max_check_fix_iterations"] = 1
        self.write_config()
        self.env.update(INITIAL_GOOD="1", FIX_MODE="blocked", TEST_SDK_READY="0")
        self.check_script.write_text(
            "import os, sys\n"
            "if os.environ.get('TEST_SDK_READY') != '1':\n"
            "    print('SDK location not found')\n"
            "    sys.exit(1)\n" + repairs.CHECK_SCRIPT)

    def state_path(self):
        return next((self.root / "runs").glob("*/KRMA-1/state.json"))

    def test_fixed_environment_retries_checks_without_another_codex_session(self):
        self.assertIn("Need SDK configuration", self.run_task(success=False).stderr)
        original = self.state()
        self.assertTrue(original["pending_check_fix"])
        self.env["TEST_SDK_READY"] = "1"
        self.run_task("run", "--local-only")
        state = self.state()
        self.assertEqual(state["phase"], "verified")
        self.assertFalse(state["pending_check_fix"])
        self.assertEqual(state["check_fix_attempts"], 1)
        self.assertEqual(state["check_fix_history"], original["check_fix_history"])
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual([check["name"] for check in self.checks()], ["first", "second"])
        self.assertNotIn("last_check_failure", state)
        self.assertNotIn("last_error", state)

    def test_failed_retry_records_current_logs_and_preserves_limit(self):
        self.run_task(success=False)
        old_log = self.state()["last_check_failure"][0]["log"]
        result = self.run_task("run", "--local-only", success=False)
        self.assertIn("лимит", result.stderr)
        state = self.state()
        self.assertNotEqual(state["last_check_failure"][0]["log"], old_log)
        self.assertEqual(state["check_fix_attempts"], 1)
        self.assertEqual(len(self.calls()), 2)
        self.assertTrue(state["pending_check_fix"])

    def test_partial_repair_and_publish_still_require_completion(self):
        self.env["ADD_REPAIR_FILE"] = "1"
        self.run_task(success=False)
        self.env["TEST_SDK_READY"] = "1"
        self.assertIn("не завершено", self.run_task("publish", success=False).stderr)
        self.assertIn("лимит", self.run_task("run", "--local-only", success=False).stderr)
        self.assertTrue(self.state()["pending_check_fix"])
        self.assertEqual(len(self.calls()), 2)
        self.assertFalse(self.checks())


if __name__ == "__main__":
    unittest.main()
