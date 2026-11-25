"""Code review and review-repair retries."""
from __future__ import annotations

import json
import time

from .checks import verify_with_fixes
from .codex import launch_codex
from .contracts import (
    RESULT_CONTRACT,
    RESULT_SCHEMA,
    REVIEW_SCHEMA,
    blocking_findings,
    merge_implementation_results,
    validate_result,
    validate_review,
)
from .events import step, emit
from .errors import TaskError, require
from .plan_context import continuation_context, plan_context
from .processes import capture
from .repository import (
    assert_scope,
    assert_unchanged_checks,
    assert_worktree,
    git,
    task_changed_paths,
)
from .storage import write_json
from .usage import event_usage


def review_once(config, state, task_text, card_path, attempt, state_path):
    worktree = assert_worktree(state)
    require(state.get("checked_tree"), "Перед review нужны успешные checks.")
    assert_unchanged_checks(state, worktree, state["checked_tree"])
    directory = attempt / f"review-{len(state.get('review_history', [])) + 1}"
    directory.mkdir()
    diff_path = directory / "checked.diff"
    diff_path.write_text(capture(["git", "diff", "--binary", "--no-renames", state["base_sha"]], worktree).stdout,
                         encoding="utf-8")
    write_json(directory / "review-context.json", {"card_file": str(card_path), "base_sha": state["base_sha"],
                                                    "diff_file": str(diff_path), "checked_tree": state["checked_tree"],
                                                    "checks": state["checks"], "plan_file": state.get("plan_path")})
    prompt = f"""Проверь реализацию задачи в текущем worktree. Следуй AGENTS.md.
Работай только с проверенным снимком; не изменяй файлы, Git index, ветку или HEAD.
Не запускай commit, push, checkout, reset или создание PR. Не повторяй checks.
Карточка задачи: {card_path}. Исходный commit: {state['base_sha']}.
{plan_context(state)}
{continuation_context(state)}
Полный diff относительно него, включая новые и удалённые файлы: {diff_path}.
Результаты checks и пути к их полным журналам: {directory / 'review-context.json'}.
Используй результаты проверок управляющего скрипта. Невозможность повторить checks
в read-only песочнице сама по себе не является блокером review.
Проверь выполнение критериев приёмки, ошибки поведения и регрессии во всём diff.
Замечания должны быть конкретными: P0/P1/P2 требуют исправления, P3 информационный.
Для каждого замечания укажи строку и объяснение; поле file — путь относительно корня текущего worktree,
например android/libs/example/File.kt. Абсолютные пути и компоненты '..' запрещены.
Если review невозможно из-за отсутствия существенных данных, верни needs_input с blockers.
При технической невозможности провести review верни blocked с конкретной причиной.
Ответь строго по переданной JSON Schema.

КАРТОЧКА ЗАДАЧИ:
{task_text}
"""
    record = {"cycle_id": state["cycle_id"], "directory": str(directory), "checked_tree": state["checked_tree"],
              "status": "running"}
    state.setdefault("review_history", []).append(record)
    write_json(state_path, state)
    try:
        try:
            result = launch_codex(config, state, directory, "review", prompt, REVIEW_SCHEMA, validate_review, "read-only")
        finally:
            # A failed or interrupted read-only session still has to leave the checked snapshot intact.
            try:
                assert_unchanged_checks(state, worktree, record["checked_tree"],
                                        message="Review изменил исходники или index. Проверьте изменения и выполните run --rerun.")
            except TaskError:
                state["requires_rerun"] = "Review"
                raise
        record["status"] = result["status"]
        record["result"] = result
        state["review_result"] = result
        if result["status"] == "reviewed":
            state["review_tree"] = record["checked_tree"]
        else:
            state.pop("review_tree", None)
        require(result["status"] == "reviewed",
                "Нужны уточнения или устранение блокеров review: " + "; ".join(result["blockers"]))
        return result
    except BaseException as exc:
        if record["status"] == "running":
            record["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        record["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        _, record["session_id"] = event_usage(directory / "events.jsonl", record["status"])
        write_json(directory / "review.json", record)
        write_json(state_path, state)


@step('Исправление замечаний review')
def fix_review_findings(config, state, task_text, card_path, attempt, state_path):
    require(not state.get("commit_sha"), "После коммита исправления review отключены.")
    worktree = assert_worktree(state)
    assert_scope(config, task_changed_paths(worktree, state))
    findings = blocking_findings(state["review_result"])
    require(findings, "Нет замечаний P0/P1/P2 для исправления.")
    count, limit = state.get("review_fix_attempts", 0), config["max_fix_iterations"]
    require(count < limit,
            f"Исчерпан лимит исправлений review ({count}/{limit}). Замечания: "
            f"{state.get('review_history', [])[-1].get('directory')}. До коммита используйте run --rerun.")
    directory = attempt / f"review-fix-{count + 1}"
    directory.mkdir()
    diff_path = directory / "current.diff"
    git(worktree, "add", "--all")
    diff_path.write_text(capture(["git", "diff", "--binary", "--no-renames", state["base_sha"]], worktree).stdout,
                         encoding="utf-8")
    findings_path = directory / "review-failure.json"
    write_json(findings_path, {"findings": findings, "review": state["review_result"],
                               "card_file": str(card_path), "diff_file": str(diff_path), "plan_file": state.get("plan_path")})
    prompt = f"""Исправь замечания последнего review из файла: {findings_path}
Текущий полный diff относительно исходного commit: {diff_path}
{plan_context(state)}
{continuation_context(state)}
Сохрани требования карточки и внеси минимальные изменения. Следуй AGENTS.md.
Не ослабляй checks. Не выполняй commit, push, checkout, reset и создание PR.
Разрешённые пути: {json.dumps(config['allow_paths'], ensure_ascii=False)}
Запрещённые пути: {json.dumps(config['deny_paths'], ensure_ascii=False)}
Если исправление невозможно, верни конкретный blocker по JSON Schema.
После исправления управляющий скрипт повторит все checks и review.
{RESULT_CONTRACT}

КАРТОЧКА ЗАДАЧИ:
{task_text}
"""
    record = {"cycle_id": state["cycle_id"], "iteration": count + 1, "directory": str(directory),
              "status": "running", "findings": findings}
    state.setdefault("review_fix_history", []).append(record)
    state.update(review_fix_attempts=count + 1, pending_review_fix=True)
    write_json(state_path, state)
    emit(kind="retry", stage="review_fix", iteration=count + 1, limit=limit, directory=str(directory))
    previous = state.get("result")
    candidate = dict(state)
    started = time.monotonic()
    try:
        result = launch_codex(config, candidate, directory, "review_fix", prompt,
                              RESULT_SCHEMA, validate_result, "workspace-write")
        assert_worktree(state)
        assert_scope(config, task_changed_paths(worktree, state))
        record["result"] = result
        require(result["status"] == "implemented" and not result["blockers"],
                "Исправление review заблокировано: " + "; ".join(result["blockers"] or [result["summary"]]))
        state["result"] = merge_implementation_results(previous, result, "Исправления review: ")
        state["phase"] = "implemented"
        state.pop("checked_tree", None)
        state.pop("checks", None)
        state.pop("reviewed_tree", None)
        state.pop("review_tree", None)
        state["pending_review_fix"] = False
        record["status"] = "implemented"
    except BaseException as exc:
        record["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        record["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        record["seconds"] = round(time.monotonic() - started, 2)
        _, record["session_id"] = event_usage(directory / "events.jsonl", record["status"])
        write_json(directory / "fix.json", record)
        write_json(state_path, state)


@step('Review')
def review_with_fixes(config, state, task_text, card_path, attempt, state_path, *, allow_sessions):
    if state.get("review_skip_reason"):
        require(not state.get("pending_review_fix"), "Незавершённое исправление review блокирует публикацию.")
        state["reviewed_tree"] = state["checked_tree"]
        emit(kind="stage", stage="review", status="skipped", reason=state["review_skip_reason"])
        return
    while True:
        require(not state.get("pending_review_fix"), "Незавершённое исправление review. Продолжите через run.")
        result = state.get("review_result")
        if (state.get("reviewed_tree") == state.get("checked_tree") and result
                and result.get("status") == "reviewed" and not blocking_findings(result)):
            state["phase"] = "reviewed" if not state.get("commit_sha") else "committed"
            return
        if not allow_sessions:
            raise TaskError("Нет актуального успешного review проверенного кода. До коммита выполните run; "
                            "для старого коммита завершите публикацию вручную.")
        if (state.get("review_tree") == state.get("checked_tree") and result
                and result.get("status") == "reviewed" and blocking_findings(result)):
            fix_review_findings(config, state, task_text, card_path, attempt, state_path)
            check_dir = attempt / f"after-review-fix-{state['review_fix_attempts']}"
            check_dir.mkdir()
            verify_with_fixes(config, state, task_text, card_path, check_dir, state_path, allow_fixes=True)
            write_json(state_path, state)
            continue
        result = review_once(config, state, task_text, card_path, attempt, state_path)
        if not blocking_findings(result):
            state["reviewed_tree"] = state["checked_tree"]
            state["phase"] = "reviewed"
            write_json(state_path, state)
            return
