"""Command orchestration and published-PR continuation."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import time

from .card import read_card
from .checks import run_steps, verify_with_fixes
from .coding import implement
from .config import resolve_profile
from .models import OperationResult, WorkflowRequest
from .events import step, emit
from .errors import require
from .github import find_pr
from .plan_context import require_planning
from .planning import planning_once
from .processes import executable_path
from .publishing import publish
from .reports import update_reports
from .repository import (
    assert_published_remote,
    assert_scope,
    assert_worktree,
    changed_paths,
    git,
    prepare,
    repository_lock,
    task_changed_paths,
)
from .review import fix_review_findings, review_with_fixes
from .state import archive_cycle, configure_cycle, migrate_state
from .storage import read_json, write_json


def start_continuation(config, state, card, args, run_dir):
    if state["phase"] != "published":
        require(state.get("continuation"), "--continue-pr требует опубликованный Draft PR.")
        require(find_pr(config, state) is not None, "Сохранённый PR не найден.")
        assert_published_remote(config, state)
        return
    require(state.get("commit_sha") and state.get("pr"), "Нет сохранённого опубликованного коммита и PR.")
    require(find_pr(config, state) is not None, "Сохранённый PR не найден.")
    # Validate remote before resetting any cycle state.
    expected = state.get("last_published_sha") or state["commit_sha"]
    require(state["commit_sha"] == expected, "Сохранённый коммит не совпадает с последним опубликованным.")
    output = git(config["repo"], "ls-remote", "--heads", config["remote"], "refs/heads/" + state["branch"])
    require(output and output.split()[0] == expected, "Удалённая ветка изменилась или отсутствует. Проверьте историю вручную.")
    worktree = assert_worktree(state)
    assert_scope(config, sorted(set(task_changed_paths(worktree, state)) |
                                set(changed_paths(worktree, state["commit_sha"]))))
    profile = resolve_profile(config, card)
    migrate_state(state)
    previous_card = (run_dir / "task.md").read_text(encoding="utf-8")
    previous_commit = state["commit_sha"]
    # configure_cycle archives the old cycle and resets only its current results.
    reset_args = replace(args, rerun=True, continue_pr=False)
    archive_cycle(state, run_dir)
    state.pop("commit_sha")
    state.pop("cycle_id", None)
    configure_cycle(config, state, profile, reset_args, run_dir)
    state.update(continuation=True, cycle_base_sha=previous_commit, previous_card_text=previous_card)
    return True


def run_workflow(args: WorkflowRequest, config) -> OperationResult:
    card_path = args.card.expanduser().resolve()
    # Загружает карточку задачи
    card, task_text = read_card(card_path)
    repo = config["repo"]
    repo_key = hashlib.sha256(str(repo).encode()).hexdigest()[:12]
    run_dir = config["runs_root"] / repo_key / card["issue"]
    state_path = run_dir / "state.json"
    if args.command == "report":
        require(state_path.is_file(), "Состояние задачи не найдено; сначала выполните run.")
        update_reports(read_json(state_path), run_dir, announce=True)
        return OperationResult("reported", run_dir)
    # Готовит каталог запуска и блокирует параллельный запуск
    with step('Проверка инструментов'):
        for executable in ("git", "gh") + ((config["codex_bin"],) if args.command == "run" else ()):
            require(executable_path(executable), f"Команда не найдена: {executable}")
    require(repo.is_dir(), "Каталог репозитория не найден.")
    for directory in (config["worktrees_root"], config["runs_root"]):
        require(not directory.is_relative_to(repo), "worktrees_root и runs_root должны находиться вне checkout.")
    run_dir.mkdir(parents=True, exist_ok=True)
    require(not run_dir.is_relative_to(config["worktrees_root"]), "runs_root должен быть отдельно от worktrees_root.")
    digest = hashlib.sha256(task_text.encode()).hexdigest()
    with repository_lock(repo):
        require(args.command == "run" or state_path.exists(), "Сначала выполните run.")

        require(not args.continue_pr or state_path.is_file(), "Для --continue-pr нужно сохранённое состояние опубликованной задачи.")
        # проверяет Git remote и получает целевую ветку
        state = prepare(config, card, run_dir, state_path)

        try:
            new_continuation = False
            if args.continue_pr:
                new_continuation = start_continuation(config, state, card, args, run_dir)
            elif state.get("continuation") and state["phase"] != "published":
                require(find_pr(config, state) is not None, "Сохранённый PR не найден.")
                assert_published_remote(config, state)
            migrate_state(state)
            if state.get("card_sha") and state["card_sha"] != digest:
                require(new_continuation or (args.rerun and not state.get("commit_sha")),
                        "Карточка изменилась. До коммита используйте run --rerun; для опубликованного Draft PR — run --continue-pr. "
                        "Если новый коммит ещё не опубликован, сначала завершите publish с прежней карточкой.")

            if state["phase"] == "published":
                pr = find_pr(config, state)
                require(pr is not None, "Сохранённый PR не найден.")
                state.pop("last_error", None)
                emit("Draft PR уже существует: " + pr["url"])
                return OperationResult(state["phase"], run_dir, assert_worktree(state), state.get("pr"))

            selected_profile = resolve_profile(config, card)
            configure_cycle(config, state, selected_profile, args, run_dir)
            migrate_state(state)

            state["card_sha"] = digest
            (run_dir / "task.md").write_text(task_text, encoding="utf-8")
            attempt = run_dir / f"attempt-{time.time_ns()}"
            attempt.mkdir()
            state["last_attempt"] = str(attempt)
            write_json(state_path, state)
            if state["phase"] == "prepared":
                require(args.command == "run", "Нет завершённой реализации. Используйте run.")

                if not state.get("setup_completed"):
                    run_steps(config["setup"], assert_worktree(state), attempt, "setup")
                    state["setup_completed"] = True
                    write_json(state_path, state)
                planning_once(config, state, task_text, card_path, attempt, state_path)
                require_planning(state)
                state["coding_started"] = True
                write_json(state_path, state)
                implement(config, state, task_text, card_path, attempt)
                write_json(state_path, state)

            require_planning(state)

            if state.get("pending_review_fix"):
                require(args.command == "run", "Незавершённое исправление review. Продолжите через run.")
                fix_review_findings(config, state, task_text, card_path, attempt, state_path)

            # запускает проверки и сохраняет результат в state.json
            verify_with_fixes(config, state, task_text, card_path, attempt, state_path,
                              allow_fixes=args.command == "run" and not state.get("commit_sha"))
            write_json(state_path, state)

            review_with_fixes(config, state, task_text, card_path, attempt, state_path,
                              allow_sessions=args.command == "run" and not state.get("commit_sha"))
            write_json(state_path, state)

            if args.local_only:
                emit("Готово локально. Worktree: " + state["worktree"])
                emit("Состояние и логи: " + str(run_dir))
                return OperationResult(state["phase"], run_dir, assert_worktree(state), state.get("pr"))

            publish(config, state, card, run_dir, state_path)
        except BaseException as exc:
            state["last_error"] = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            try:
                update_reports(state, run_dir, announce=True)
            finally:
                with step("Сохранение состояния"):
                    write_json(state_path, state)

    return OperationResult(state["phase"], run_dir, assert_worktree(state), state.get("pr"))
