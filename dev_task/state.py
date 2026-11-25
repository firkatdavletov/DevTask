"""Workflow-state migration and cycle lifecycle."""
from __future__ import annotations

import time
from pathlib import Path

from .errors import TaskError, require
from .models import RunRequest
from .storage import read_json


def legacy_coding_started(state, run_dir):
    if "coding_started" in state:
        return bool(state["coding_started"])
    if state["phase"] != "prepared":
        return True
    cycle_id = state.get("cycle_id")
    ambiguous = False
    had_logs = False
    for directory in run_dir.glob("attempt-*"):
        usage_path = directory / "usage.json"
        events = directory / "events.jsonl"
        if not usage_path.exists() and not events.exists():
            continue
        had_logs = True
        try:
            usage = read_json(usage_path)
        except (OSError, ValueError):
            usage = {}
        if not isinstance(usage, dict):
            usage = {}
        if usage.get("stage", "coding") != "coding":
            continue
        if cycle_id and usage.get("cycle_id") == cycle_id:
            return True
        if not cycle_id or not usage.get("cycle_id"):
            ambiguous = True
    return ambiguous or (not had_logs and bool(state.get("result")))


def legacy_setup_completed(config, state):
    if not config["setup"]:
        return True
    try:
        steps = read_json(Path(state["last_attempt"]) / "setup.json")
        return (len(steps) == len(config["setup"]) and
                all(item["exit_code"] == 0 and item["argv"] == expected["argv"] and item["name"] == expected["name"]
                    for item, expected in zip(steps, config["setup"])))
    except (KeyError, OSError, ValueError, TypeError):
        return False


def migrate_state(state):
    require(state.get("workflow_version", 0) <= 4, "Состояние создано более новой версией dev-task.")
    if state.get("result"):
        state["result"].setdefault("verification_limits", [])
    state.setdefault("cycle_base_sha", state["base_sha"])
    if state.get("phase") == "published":
        state.setdefault("last_published_sha", state.get("commit_sha"))
    if state.get("workflow_version") == 3:
        state["workflow_version"] = 4


def archive_cycle(state, run_dir):
    if not state.get("cycle_id"):
        return
    keys = ("cycle_id", "cycle_base_sha", "commit_sha", "card_sha", "result", "checks", "review_result",
            "execution_profile", "plan_path", "phase", "cycle_outcome", "check_fix_attempts", "review_fix_attempts")
    record = {key: state[key] for key in keys if key in state}
    path = run_dir / "task.md"
    record["card_text"] = path.read_text(encoding="utf-8") if path.is_file() else ""
    state.setdefault("cycle_history", []).append(record)


def configure_cycle(config, state, profile, args: RunRequest, run_dir, *, legacy_text=None):
    """Upgrade in place without adding planning after an old cycle's coding."""
    require(state.get("workflow_version", 0) <= 4, "Состояние создано более новой версией dev-task.")
    if args.rerun:
        require(not state.get("commit_sha"), "После коммита --rerun отключён. Используйте publish.")
        archive_cycle(state, run_dir)
        state.update(phase="prepared", execution_profile=profile, workflow_version=4,
                     cycle_id=f"cycle-{time.time_ns()}", setup_completed=False, coding_started=False,
                     check_fix_attempts=0, review_fix_attempts=0, pending_check_fix=False, pending_review_fix=False,
                     review_skip_reason="--skip-review" if args.skip_review else "профиль сложности" if profile["review"] is None else None)
        for key in ("last_check_failure", "last_error", "requires_rerun", "reviewed_tree", "review_tree", "review_result",
                    "plan_path", "plan_sha256", "planning_result", "checked_tree", "checks", "result",
                    "session_id", "commit_message", "commit_message_tree", "commit_message_issue", "commit_message_base", "cycle_outcome"):
            state.pop(key, None)
        state["planning_skip_reason"] = ("--skip-planning" if args.skip_planning else
                                         "профиль сложности" if profile["planning"] is None else None)
        state["planning_status"] = "skipped" if state["planning_skip_reason"] else "pending"
        return
    require(not state.get("requires_rerun"),
            f'{state.get("requires_rerun")} изменил рабочий снимок. Проверьте изменения и выполните run --rerun.')
    previous = state.get("execution_profile")
    if state.get("commit_sha") and not previous:
        raise TaskError("Старое состояние уже закоммичено без review. Завершите публикацию вручную.")
    if previous:
        if "text" not in previous:
            previous = {**previous, "text": legacy_text}
        compared = profile if state.get("workflow_version", 0) >= 3 else {k: v for k, v in profile.items() if k != "planning"}
        if state.get("commit_sha"):
            compared = {**compared, "text": previous["text"]}
        require(previous == compared,
                "Профиль моделей или сложность изменились. До коммита используйте run --rerun.")
        require(not args.skip_review or state.get("review_skip_reason"),
                "Чтобы изменить режим review, используйте run --rerun --skip-review.")
    if state.get("workflow_version", 0) >= 3:
        require(bool(previous), "Не сохранён профиль цикла. До коммита используйте run --rerun.")
        require(not args.skip_planning or state.get("planning_skip_reason"),
                "Чтобы изменить режим planning, используйте run --rerun --skip-planning.")
        state["execution_profile"] = previous
        return
    started = legacy_coding_started(state, run_dir)
    state["execution_profile"] = {**profile, "text": previous["text"]} if previous else profile
    state["workflow_version"] = 3
    state.setdefault("cycle_id", f"cycle-{time.time_ns()}")
    state.setdefault("review_skip_reason", "--skip-review" if args.skip_review else
                     "профиль сложности" if profile["review"] is None else None)
    state.setdefault("review_fix_attempts", 0)
    state.setdefault("pending_review_fix", False)
    state.setdefault("check_fix_attempts", 0)
    state["coding_started"] = started
    state["setup_completed"] = started or legacy_setup_completed(config, state)
    state["planning_skip_reason"] = ("цикл начат до добавления planning" if started else
                                     "--skip-planning" if args.skip_planning else
                                     "профиль сложности" if profile["planning"] is None else None)
    state["planning_status"] = "skipped" if state["planning_skip_reason"] else "pending"
    for key in ("plan_path", "plan_sha256", "planning_result"):
        state.pop(key, None)
