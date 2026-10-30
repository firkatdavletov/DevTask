"""Usage reporting against local Git, fake exec and a JSON-RPC app-server."""
import json
from pathlib import Path
import shutil
import unittest

import test_check_repairs as repairs


class UsageReportTest(unittest.TestCase):
    # Reuse the local repository fixture without inheriting its independent test cases.
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
        self.env["USAGE_MODE"] = "normal"

    def report(self):
        return json.loads((self.state_path().parent / "report.json").read_text())

    def report_text(self):
        return (self.state_path().parent / "report.md").read_text()

    def snapshot(self, percent, reset=2000000000, secondary=None):
        windows = {"primary": {"usedPercent": percent, "windowDurationMins": 300, "resetsAt": reset}}
        if secondary is not None:
            windows["secondary"] = {"usedPercent": secondary, "windowDurationMins": 10080, "resetsAt": 2100000000}
        return {"rateLimitsByLimitId": {"codex": windows}}

    def test_stages_tokens_cache_and_subscription_deltas(self):
        self.run_task("run", "--local-only")
        report = self.report()
        self.assertEqual(report["total"]["tokens"], {"input_tokens": 150, "output_tokens": 25,
                         "cached_input_tokens": 90, "total_tokens": 175, "status": "complete"})
        self.assertEqual(report["stages"]["coding"]["tokens"]["total_tokens"], 120)
        self.assertEqual(report["stages"]["check_fix"]["tokens"]["total_tokens"], 55)
        self.assertEqual(report["total"]["limit_usage"][0]["known_percentage_points"], 6)
        self.assertEqual([s["limit_usage"][0]["consumed_percentage_points"] for s in report["sessions"]], [3, 3])
        self.assertEqual(report["sessions"][0]["limits_before"]["buckets"]["codex"]["primary"]["usedPercent"], 10)
        self.assertEqual(report["sessions"][0]["session_id"], "session-1")
        self.assertEqual(report["sessions"][1]["session_id"], "session-2")
        for session in report["sessions"]:
            self.assertTrue(Path(session["directory"], "usage.json").is_file())
            self.assertTrue(Path(session["events_log"]).is_file())
        requests = [json.loads(line)["method"] for line in (self.root / "rpc-calls.jsonl").read_text().splitlines()]
        self.assertEqual(requests, ["initialize", "initialized", "account/rateLimits/read"] * 4)
        self.assertEqual(self.state()["usage"]["total"], report["total"])
        self.assertIn("10% → 13%", self.report_text())

    def test_repeated_run_report_and_rerun_do_not_double_count(self):
        self.run_task("run", "--local-only")
        self.run_task("run", "--local-only")
        self.run_task("report")
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 175)
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 350)
        self.assertEqual(len(self.report()["sessions"]), 4)

    def test_multiple_repairs_are_counted(self):
        self.env.update(FIX_MODE="noop", CHECK_MODE="always")
        self.run_task("run", "--local-only", success=False)
        report = self.report()
        self.assertEqual(report["stages"]["check_fix"]["session_count"], 3)
        self.assertEqual(report["total"]["tokens"]["total_tokens"], 285)
        self.assertIn("Исчерпан лимит", report["last_error"])

    def test_multiple_turns_are_summed(self):
        self.env.update(USAGE_MODE="multiple", INITIAL_GOOD="1")
        self.run_task("run", "--local-only")
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 240)
        self.assertEqual(self.report()["total"]["tokens"]["status"], "complete")

    def test_missing_usage_is_unknown_and_unused_stage_zero(self):
        self.env.update(USAGE_MODE="missing", INITIAL_GOOD="1")
        self.run_task("run", "--local-only")
        self.assertEqual(self.report()["total"]["tokens"]["status"], "unknown")
        self.assertIsNone(self.report()["total"]["tokens"]["total_tokens"])
        self.assertEqual(self.report()["stages"]["check_fix"]["tokens"]["total_tokens"], 0)

    def test_damaged_json_keeps_known_usage(self):
        self.env.update(USAGE_MODE="damaged", INITIAL_GOOD="1")
        self.run_task("run", "--local-only")
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 120)
        self.assertEqual(self.report()["total"]["tokens"]["status"], "partial")

    def test_invalid_counts_keep_only_known_nonnegative_integers(self):
        self.env.update(USAGE_MODE="invalid_counts", INITIAL_GOOD="1")
        self.run_task("run", "--local-only")
        tokens = self.report()["total"]["tokens"]
        self.assertEqual(tokens["input_tokens"], 100)
        self.assertIsNone(tokens["output_tokens"])
        self.assertIsNone(tokens["cached_input_tokens"])
        self.assertEqual(tokens["total_tokens"], 100)
        self.assertEqual(tokens["status"], "partial")

    def test_blocker_and_process_failure_preserve_accounting_and_result(self):
        self.env["FIX_MODE"] = "blocked"
        self.run_task("run", "--local-only", success=False)
        self.assertEqual(self.report()["sessions"][1]["status"], "blocked")
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 175)
        self.assertEqual(self.state()["result"]["summary"], "Original implementation")
        self.env["FIX_MODE"] = "fail"
        self.run_task("run", "--local-only", success=False)
        report = self.report()
        self.assertEqual(report["sessions"][-1]["exit_code"], 9)
        self.assertEqual(report["sessions"][-1]["status"], "failed")
        self.assertEqual(report["total"]["tokens"]["total_tokens"], 175)
        self.assertEqual(report["total"]["tokens"]["status"], "partial")

    def test_timeout_and_interruption_save_partial_usage_and_resume(self):
        self.cfg["codex_timeout_seconds"] = 1
        self.write_config()
        self.env.update(FIX_MODE="timeout", PARTIAL_USAGE="1")
        self.run_task("run", "--local-only", success=False)
        report = self.report()
        self.assertEqual(report["sessions"][-1]["status"], "timed_out")
        self.assertEqual(report["total"]["tokens"]["total_tokens"], 129)
        self.assertEqual(report["total"]["tokens"]["status"], "partial")
        self.env["FIX_MODE"] = "interrupt"
        result = self.run_task("run", "--local-only", success=False)
        self.assertEqual(result.returncode, 130)
        self.assertEqual(self.report()["sessions"][-1]["status"], "interrupted")
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 138)
        self.env.pop("FIX_MODE")
        self.env.pop("PARTIAL_USAGE")
        self.run_task("run", "--local-only")
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 193)

    def test_initial_failure_is_reported(self):
        self.env["INITIAL_MODE"] = "fail"
        self.run_task("run", "--local-only", success=False)
        self.assertEqual(self.report()["sessions"][0]["status"], "failed")
        self.assertEqual(self.report()["total"]["tokens"]["status"], "unknown")

    def test_app_server_error_and_empty_limits_do_not_block_workflow(self):
        self.env.update(LIMIT_MODE="unavailable", INITIAL_GOOD="1")
        self.run_task("run", "--local-only")
        self.assertEqual(self.report()["total"]["sessions_without_limit_data"], 1)
        self.assertIn("app-server", self.report_text())
        self.env["LIMIT_MODE"] = "no_limits"
        self.run_task("run", "--local-only", "--rerun")
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 240)
        self.assertEqual(self.report()["total"]["sessions_without_limit_data"], 2)

    def test_app_server_timeout_is_bounded_and_nonfatal(self):
        self.env.update(LIMIT_TIMEOUT_CALL="1", INITIAL_GOOD="1")
        self.run_task("run", "--local-only")
        session = self.report()["sessions"][0]
        self.assertIn("10 секунд", session["limits_before"]["reason"])
        self.assertIsNone(session["limit_usage"][0]["consumed_percentage_points"])
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 120)

    def test_legacy_limit_view(self):
        self.env.update(LIMIT_MODE="legacy", INITIAL_GOOD="1")
        self.run_task("run", "--local-only")
        self.assertEqual(self.report()["total"]["limit_usage"][0]["known_percentage_points"], 3)

    def test_multiple_windows_and_buckets(self):
        first, last = self.snapshot(20, secondary=30), self.snapshot(23, secondary=31)
        for snap, value in ((first, 40), (last, 42)):
            snap["rateLimitsByLimitId"]["other"] = self.snapshot(value)["rateLimitsByLimitId"]["codex"]
        self.env.update(INITIAL_GOOD="1", LIMIT_SNAPSHOTS=json.dumps([first, last]))
        self.run_task("run", "--local-only")
        changes = self.report()["sessions"][0]["limit_usage"]
        self.assertEqual([c["consumed_percentage_points"] for c in changes], [3, 1, 2])

    def test_reset_and_decrease_are_unknown(self):
        self.env.update(INITIAL_GOOD="1", LIMIT_SNAPSHOTS=json.dumps([
            self.snapshot(20), self.snapshot(2, reset=2000001000),
            self.snapshot(30, reset=2000001000), self.snapshot(29, reset=2000001000)]))
        self.run_task("run", "--local-only")
        self.run_task("run", "--local-only", "--rerun")
        sessions = self.report()["sessions"]
        self.assertTrue(all(s["limit_usage"][0]["consumed_percentage_points"] is None for s in sessions))
        self.assertIn("сбросилось", sessions[0]["limit_usage"][0]["reason"])
        self.assertIn("уменьшился", sessions[1]["limit_usage"][0]["reason"])
        self.assertEqual(len(self.report()["total"]["limit_usage"]), 2)
        self.assertTrue(all(w["known_percentage_points"] is None for w in self.report()["total"]["limit_usage"]))

    def test_recovery_ignores_duplicate_paths_and_marks_incomplete_turns(self):
        self.env["INITIAL_GOOD"] = "1"
        self.run_task("run", "--local-only")
        session = self.report()["sessions"][0]
        directory = Path(session["directory"])
        (directory.parent / "attempt-alias").symlink_to(directory, target_is_directory=True)
        with Path(session["events_log"]).open("a") as events:
            events.write(json.dumps({"type": "turn.started"}) + "\n")
            events.write(json.dumps({"type": "turn.completed"}) + "\n")
        self.run_task("report")
        report = self.report()
        self.assertEqual(report["total"]["session_count"], 1)
        self.assertEqual(report["total"]["tokens"]["total_tokens"], 120)
        self.assertEqual(report["total"]["tokens"]["status"], "partial")

    def test_conflicting_exec_profile_is_rejected(self):
        self.env["INITIAL_GOOD"] = "1"
        self.cfg["codex_args"] = ["--profile", "different-account"]
        self.write_config()
        result = self.run_task("run", "--local-only", success=False)
        self.assertIn("codex_args", result.stderr)
        self.assertFalse((self.root / "limit-call-count").exists())

    def test_old_published_task_report_is_offline_and_recovers_tokens(self):
        self.run_task("run")
        run_dir = self.state_path().parent
        state = self.state()
        state.pop("usage")
        self.state_path().write_text(json.dumps(state))
        for path in run_dir.glob("attempt-*/**/usage.json"):
            path.unlink()
        for name in ("report.json", "report.md"):
            (run_dir / name).unlink()
        before_calls, before_checks = self.calls(), self.checks()
        before_limits = (self.root / "limit-call-count").read_text()
        for name in ("git", "gh", "codex"):
            (self.root / "bin" / name).write_text("#!/bin/sh\nexit 99\n")
        shutil.rmtree(self.repo)
        self.run_task("report")
        report = self.report()
        self.assertEqual(report["phase"], "published")
        self.assertEqual(report["total"]["tokens"]["total_tokens"], 175)
        self.assertEqual(report["total"]["sessions_without_limit_data"], 2)
        self.assertEqual(self.calls(), before_calls)
        self.assertEqual(self.checks(), before_checks)
        self.assertEqual((self.root / "limit-call-count").read_text(), before_limits)
        self.assertEqual(self.state(), state)
        self.run_task("report")
        self.assertEqual(self.report(), report)

    def test_published_repeat_creates_no_new_usage_or_pr_body_statistics(self):
        self.run_task("run")
        self.run_task("run")
        self.assertEqual(self.report()["total"]["session_count"], 2)
        self.assertNotIn("Токены", (self.state_path().parent / "pr-body.md").read_text())

    def test_setup_failure_has_no_model_usage(self):
        self.cfg["setup"] = [{"name": "broken", "argv": ["false"]}]
        self.write_config()
        self.run_task("run", "--local-only", success=False)
        self.assertEqual(self.report()["total"]["session_count"], 0)
        self.assertEqual(self.report()["total"]["tokens"]["total_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
