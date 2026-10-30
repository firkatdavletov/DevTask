"""Project commands, verification and check-repair retries."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .coding import implement
from .console import say
from .contracts import merge_implementation_results
from .errors import CheckFailed, TaskError, require
from .processes import capture, execute
from .repository import (
    assert_scope,
    assert_unchanged_checks,
    assert_worktree,
    git,
    task_changed_paths,
)
from .storage import write_json


def run_steps(steps, worktree, attempt, prefix):
    evidence = []
    for i, step in enumerate(steps, 1):
        say(f"  {prefix}: {step['name']}")
        log = attempt / f"{prefix}-{i}.log"
        started = time.monotonic()
        try:
            code = execute(step["argv"], worktree, log, step["timeout_seconds"])
        except subprocess.TimeoutExpired:
            code = 124
        evidence.append({"name": step["name"], "argv": step["argv"], "exit_code": code,
                         "seconds": round(time.monotonic() - started, 2), "log": str(log)})
        write_json(attempt / f"{prefix}.json", evidence)
        if code != 0 and prefix == "check":
            raise CheckFailed(f"{step['name']}: код {code}. Лог: {log}", evidence)
        require(code == 0, f"{step['name']}: код {code}. Лог: {log}")
    return evidence


def verify(config, state, attempt):
    state.pop("checked_tree", None)
    state.pop("checks", None)
    state["phase"] = "committed" if state.get("commit_sha") else "implemented"
    worktree = assert_worktree(state)
    paths = task_changed_paths(worktree, state)
    require(paths or state.get("continuation"), "Нет изменений относительно исходной целевой ветки; PR не создан.")
    assert_scope(config, paths)
    # Stage exactly the candidate revision. Checks must leave this snapshot unchanged.
    git(worktree, "add", "--all")
    expected_tree = git(worktree, "write-tree")
    diff_check = capture(["git", "diff", "--cached", "--check"], worktree, ok=False)
    if diff_check.returncode != 0:
        log = attempt / "diff-check.log"
        log.write_text(diff_check.stdout + diff_check.stderr, encoding="utf-8")
        evidence = [{"name": "git diff --check", "argv": ["git", "diff", "--cached", "--check"],
                     "exit_code": diff_check.returncode, "log": str(log)}]
        write_json(attempt / "check.json", evidence)
        raise CheckFailed(f"git diff --check обнаружил ошибки пробелов или маркеры конфликта. Лог: {log}", evidence)
    try:
        evidence = run_steps(config["checks"], worktree, attempt, "check")
    except CheckFailed:
        # A failed build is repairable only if the checks did not alter the candidate revision.
        assert_unchanged_checks(state, worktree, expected_tree)
        raise
    assert_unchanged_checks(state, worktree, expected_tree)
    state.update(phase="verified" if not state.get("commit_sha") else "committed", checks=evidence, checked_tree=expected_tree)


def fix_failed_checks(config, state, task_text, card_path, attempt, state_path):
    require(not state.get("commit_sha"), "После коммита автоисправление checks отключено. Проверьте изменения вручную.")
    worktree = assert_worktree(state)
    assert_scope(config, task_changed_paths(worktree, state))
    count = state.get("check_fix_attempts", 0)
    limit = config["max_check_fix_iterations"]
    logs = ", ".join(item["log"] for item in state["last_check_failure"] if item["exit_code"] != 0)
    require(limit > 0, "Автоисправление checks отключено (max_check_fix_iterations=0). Логи: " + logs)
    require(count < limit,
            f"Исчерпан лимит автоисправлений checks ({count}/{limit}). Логи: {logs}. Исправьте код вручную "
            "либо начните новый цикл через run --rerun.")
    directory = attempt / f"check-fix-{count + 1}"
    directory.mkdir()
    record = {"iteration": count + 1, "directory": str(directory), "status": "running",
              "failure": state["last_check_failure"]}
    state.setdefault("check_fix_history", []).append(record)
    state.update(check_fix_attempts=count + 1, pending_check_fix=True)
    write_json(state_path, state)  # Failed/interrupted sessions also consume the persisted budget.
    previous_result = state.get("result")
    candidate_state = dict(state)
    started = time.monotonic()
    say(f"Автоисправление checks: {count + 1}/{limit}")
    try:
        # Include any new files from an interrupted fix in the next session's diff.
        git(worktree, "add", "--all")
        implement(config, candidate_state, task_text, card_path, directory, check_failure=record["failure"])
        assert_scope(config, task_changed_paths(worktree, state))
        record["result"] = candidate_state["result"]
        state.update(result=candidate_state["result"], phase="implemented")
        if candidate_state.get("session_id"):
            state["session_id"] = candidate_state["session_id"]
        if previous_result:
            state["result"] = merge_implementation_results(previous_result, state["result"], "Исправления checks: ")
        state["pending_check_fix"] = False
        record["status"] = "implemented"
    except BaseException as exc:
        record["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        record["error"] = type(exc).__name__ + ": " + str(exc)
        if candidate_state.get("result") is not previous_result:
            record["result"] = candidate_state["result"]
        raise
    finally:
        record["seconds"] = round(time.monotonic() - started, 2)
        events = directory / "events.jsonl"
        if events.exists():
            for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get("type") == "thread.started":
                        record["session_id"] = event.get("thread_id")
                except ValueError:
                    continue
        write_json(directory / "fix.json", record)
        write_json(state_path, state)


def verify_with_fixes(config, state, task_text, card_path, attempt, state_path, *, allow_fixes):
    if state.get("pending_check_fix"):
        require(allow_fixes, "Исправление checks не завершено. Продолжите через run до публикации.")
        worktree = assert_worktree(state)
        assert_scope(config, task_changed_paths(worktree, state))
        git(worktree, "add", "--all")
        history = state.get("check_fix_history", [])
        previous_diff = Path(history[-1]["directory"]) / "current.diff" if history else None
        current_diff = capture(["git", "diff", "--binary", "--no-renames", state["base_sha"]], worktree).stdout
        # An unchanged candidate can be retried after fixing the environment. Partial
        # source edits from an unfinished repair still require that repair to finish.
        if previous_diff and previous_diff.is_file() and previous_diff.read_text(encoding="utf-8") == current_diff:
            say("Повтор обязательных проверок перед продолжением автоисправления checks.")
        else:
            fix_failed_checks(config, state, task_text, card_path, attempt, state_path)
    round_number = 0
    while True:
        check_dir = attempt / f"checks-{round_number}"
        check_dir.mkdir()
        try:
            verify(config, state, check_dir)
        except CheckFailed as exc:
            state["last_check_failure"] = exc.evidence
            write_json(state_path, state)
            if not allow_fixes:
                hint = (" После коммита автоисправление checks отключено; требуется ручное исправление."
                        if state.get("commit_sha") else " Автоисправление доступно через run до создания коммита.")
                raise TaskError(str(exc) + hint) from exc
            fix_failed_checks(config, state, task_text, card_path, attempt, state_path)
            round_number += 1
        else:
            state["pending_check_fix"] = False
            state.pop("last_check_failure", None)
            state.pop("last_error", None)
            return
