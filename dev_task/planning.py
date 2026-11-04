"""Read-only planning and accepted-plan persistence."""
from __future__ import annotations

import contextlib
import os
import subprocess

from .codex import launch_codex
from .contracts import PLANNING_SCHEMA, validate_planning
from .errors import require
from .events import emit
from .plan_context import continuation_context, require_planning
from .repository import assert_worktree, readonly_snapshot
from .storage import file_digest, read_json, write_json
from .usage import event_usage


def planning_once(config, state, task_text, card_path, attempt, state_path):
    if state["planning_status"] == "skipped":
        emit(kind="stage", stage="planning", status="skipped", reason=state.get("planning_skip_reason"))
        return
    if state["planning_status"] == "planned":
        require_planning(state)
        emit(kind="stage", stage="planning", status="reused", plan_path=state.get("plan_path"))
        return
    worktree = assert_worktree(state)
    snapshot = readonly_snapshot(worktree)
    directory = attempt / f"planning-{len(state.get('planning_history', [])) + 1}"
    directory.mkdir()
    write_json(directory / "snapshot-before.json", snapshot)
    prompt = f"""Спланируй выполнение задачи в текущем worktree. Следуй AGENTS.md.
Изучи проект, существующие реализации и текущие изменения относительно {state['base_sha']}.
{continuation_context(state)}
Составь конкретный план изменений и проверок всех критериев приёмки.
Карточка и вложения: {card_path.parent}. Исходная карточка: {card_path}.
Не изменяй файлы, права доступа, Git index, ветку или HEAD. Не выполняй commit/push/checkout/reset.
Не создавай plan.md самостоятельно: управляющий скрипт сохранит поле plan твоего результата.
Если требований недостаточно, верни needs_input с вопросами в blockers.
При технической невозможности верни blocked с конкретной причиной.
Иначе верни planned, непустой план и пустой список blockers по переданной JSON Schema.

КАРТОЧКА ЗАДАЧИ:
{task_text}
"""
    record = {"cycle_id": state["cycle_id"], "directory": str(directory), "status": "running"}
    state.setdefault("planning_history", []).append(record)
    write_json(state_path, state)
    try:
        try:
            result = launch_codex(config, state, directory, "planning", prompt,
                                  PLANNING_SCHEMA, validate_planning, "read-only")
        finally:
            assert_worktree(state)
            require(readonly_snapshot(worktree) == snapshot,
                    "Planning изменил исходники, новые файлы, права доступа или index. Проверьте изменения вручную.")
        record.update(status=result["status"], result=result)
        require(result["status"] == "planned",
                "Нужны уточнения или устранение блокеров planning: " + "; ".join(result["blockers"]))
        plan_path = directory / "plan.md"
        temporary = directory / "plan.md.tmp"
        temporary.write_text(result["plan"].rstrip() + "\n", encoding="utf-8")
        os.replace(temporary, plan_path)
        state.update(planning_status="planned", plan_path=str(plan_path), plan_sha256=file_digest(plan_path),
                     planning_result=result)
        record["plan_path"] = str(plan_path)
    except BaseException as exc:
        if record["status"] not in ("blocked", "needs_input"):
            record["status"] = ("interrupted" if isinstance(exc, KeyboardInterrupt) else
                                "timed_out" if isinstance(exc, subprocess.TimeoutExpired) else "failed")
        record["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        _, record["session_id"] = event_usage(directory / "events.jsonl", record["status"])
        with contextlib.suppress(OSError, ValueError):
            usage_path = directory / "usage.json"
            usage = read_json(usage_path)
            usage["status"] = record["status"]
            if record.get("error"):
                usage["error"] = record["error"]
            if record.get("plan_path"):
                usage["plan_path"] = record["plan_path"]
            write_json(usage_path, usage)
        write_json(directory / "planning.json", record)
        write_json(state_path, state)
