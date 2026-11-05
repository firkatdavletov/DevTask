"""Offline session recovery and JSON/Markdown usage reports."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from .events import step, emit
from .storage import read_json, write_json
from .usage import STAGE_NAMES, TOKEN_FIELDS, aggregate_usage, event_usage, limit_changes


def report_sessions(run_dir):
    directories = set()
    for pattern in ("attempt-*/**/events.jsonl", "attempt-*/**/usage.json"):
        directories.update(p.parent.resolve() for p in run_dir.glob(pattern))
    sessions = []
    for directory in sorted(directories):
        stage = ("planning" if directory.name.startswith("planning-") else
                 "commit_message" if directory.name.startswith("commit-message-") else
                 "review_fix" if directory.name.startswith("review-fix-") else
                 "review" if directory.name.startswith("review-") else
                 "check_fix" if directory.name.startswith("check-fix-") else "coding")
        record = {"directory": str(directory), "stage": stage,
                  "status": "unknown", "recovered": True}
        try:
            metadata = read_json(directory / "usage.json")
            if isinstance(metadata, dict):
                record.update(metadata)
        except (OSError, ValueError):
            for name in ("result.json", "fix.json"):
                try:
                    result = read_json(directory / name)
                    if isinstance(result, dict):
                        record["status"] = result.get("status", "unknown")
                except (OSError, ValueError):
                    pass
        record["events_log"] = str(directory / "events.jsonl")
        record["stderr_log"] = str(directory / "codex.stderr.log")
        record["tokens"], session_id = event_usage(directory / "events.jsonl", record["status"])
        record["session_id"] = session_id or record.get("session_id")
        record["limit_usage"] = limit_changes(record.get("limits_before", {}), record.get("limits_after", {}))
        if not record["limit_usage"]:
            record["limit_reason"] = (record.get("limits_after", {}).get("reason")
                                      or "Исторические снимки лимитов отсутствуют.")
        sessions.append(record)
    def order(session):
        started = session.get("started_at")
        if type(started) not in (int, float):
            attempt = Path(session["directory"])
            while not attempt.name.startswith("attempt-") and attempt != attempt.parent:
                attempt = attempt.parent
            try:
                started = int(attempt.name.removeprefix("attempt-")) / 1_000_000_000
            except ValueError:
                started = 0
        return started, session["directory"]
    return sorted(sessions, key=order)


def token_text(tokens):
    value = tokens["total_tokens"]
    return "неизвестно" if value is None else str(value) + (" (неполные данные)" if tokens["status"] != "complete" else "")


@step('Формирование отчёта')
def update_reports(state, run_dir, *, announce=False):
    if state.get("mode") == "branch_review":
        return update_branch_review_report(state, run_dir)
    # A separate short lock also permits offline report generation without invoking Git.
    with (run_dir / "usage.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sessions = report_sessions(run_dir)
        review_history = state.get("review_history", [])
        quality = []
        for cycle_id in dict.fromkeys(item.get("cycle_id", "legacy") for item in review_history):
            reviews = [item for item in review_history if item.get("cycle_id", "legacy") == cycle_id]
            completed = [item for item in reviews if item.get("result", {}).get("status") == "reviewed"]
            p1_p2 = lambda item: sum(f.get("priority") in ("P1", "P2") for f in item["result"]["findings"])
            quality.append({"cycle_id": cycle_id,
                            "first_p1_p2": p1_p2(completed[0]) if completed else None,
                            "last_p1_p2": p1_p2(completed[-1]) if completed else None,
                            "reviews": reviews})
        report = {"version": 3, "issue": state["issue"], "phase": state["phase"], "last_error": state.get("last_error"),
                  "cycle_history": state.get("cycle_history", []),
                  "cycle_base_sha": state.get("cycle_base_sha"), "last_published_sha": state.get("last_published_sha"),
                  "verification_limits": state.get("result", {}).get("verification_limits", []),
                  "total": aggregate_usage(sessions),
                  "stages": {stage: aggregate_usage([s for s in sessions if s["stage"] == stage]) for stage in STAGE_NAMES},
                  "sessions": sessions, "profile": state.get("execution_profile"),
                  "planning_status": state.get("planning_status"),
                  "planning_skip_reason": state.get("planning_skip_reason"),
                  "plan_path": state.get("plan_path"), "planning_history": state.get("planning_history", []),
                  "review_skip_reason": state.get("review_skip_reason"),
                  "quality_status": "assessed" if any(item.get("cycle_id") == state.get("cycle_id")
                                                      and item.get("result", {}).get("status") == "reviewed"
                                                      for item in review_history) else "not_assessed",
                  "review_fix_attempts": state.get("review_fix_attempts", 0),
                  "review_fix_started": len(state.get("review_fix_history", [])),
                  "review_fix_completed": sum(item.get("status") == "implemented" for item in state.get("review_fix_history", [])),
                  "quality": quality,
                  "limit_note": "Оценка изменения общих лимитов аккаунта; параллельные задачи и задержки обновления могут влиять на результат."}
        write_json(run_dir / "report.json", report)
        lines = [f"# Расход Codex: {state['issue']}", "", f"Состояние: {state['phase']}",
                 f"Токенов всего: {token_text(report['total']['tokens'])}", "", report["limit_note"], "",
                 "Кешированные входные токены уже входят во входные. Итог = входные + выходные.", "",
                 "| Этап | Сессии | Входные | Выходные | Кешированные входные | Всего |",
                 "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for label, summary in [*[(STAGE_NAMES[k], v) for k, v in report["stages"].items()], ("Вся задача", report["total"])]:
            t = summary["tokens"]
            fields = [str(t[k]) if t[k] is not None else "неизвестно" for k in TOKEN_FIELDS]
            lines.append(f"| {label} | {summary['session_count']} | {fields[0]} | {fields[1]} | {fields[2]} | {token_text(t)} |")
        lines += ["", "## Лимиты по этапам и задаче", ""]
        for label, summary in [("Задача", report["total"]), *[(STAGE_NAMES[k], v) for k, v in report["stages"].items()]]:
            lines += [f"**{label}**", ""]
            for window in summary["limit_usage"]:
                known = window["known_percentage_points"]
                amount = "неизвестен" if known is None else f"{known:g} п.п."
                lines.append(f"- {window['limit_id']} / {window['window']}: окно {window['window_duration_mins']} мин., "
                             f"сброс Unix {window['resets_at']}; известный расход {amount}; "
                             f"неизвестных интервалов: {window['unknown_intervals']}.")
            if summary["sessions_without_limit_data"]:
                lines.append(f"- Проценты неизвестны для сессий: {summary['sessions_without_limit_data']}.")
            if not summary["session_count"]:
                lines.append("- Вызовов Codex не было: расход 0.")
            lines.append("")
        if report["verification_limits"]:
            lines += ["## Ограничения проверок агента", ""]
            lines.extend("- " + limit for limit in report["verification_limits"])
            lines.append("")
        for cycle in state.get("cycle_history", []):
            lines += [f"Завершённый цикл {cycle['cycle_id']}: {cycle.get('commit_sha') or 'без коммита'}.", ""]
        lines += ["## Сессии", ""]
        if report["profile"]:
            lines += ["Профиль: " + json.dumps(report["profile"], ensure_ascii=False), ""]
        if report["planning_skip_reason"]:
            lines += ["Planning пропущен: " + report["planning_skip_reason"], ""]
        if report["plan_path"]:
            lines += [f"Принятый план: [plan.md]({report['plan_path']})", ""]
        for planning in report["planning_history"]:
            lines += [f"Planning, цикл {planning['cycle_id']}: {planning['status']}."]
            if planning.get("result"):
                lines += [planning["result"]["summary"], ""]
                lines.extend("- " + blocker for blocker in planning["result"]["blockers"])
            if planning.get("plan_path"):
                lines.append(f"План цикла: [plan.md]({planning['plan_path']})")
            if planning.get("error"):
                lines.append("Причина остановки: " + planning["error"])
            lines.append("")
        if report["review_skip_reason"]:
            lines += ["Качество не оценивалось: " + report["review_skip_reason"], ""]
        elif report["quality_status"] == "not_assessed":
            lines += ["Качество не оценивалось: завершённого review нет.", ""]
        if report["review_fix_started"]:
            lines += [f"Исправлений после review: начато {report['review_fix_started']}, "
                      f"завершено {report['review_fix_completed']}.", ""]
        for cycle in quality:
            lines += [f"Review, цикл {cycle['cycle_id']}: P1/P2 в первом — {cycle['first_p1_p2']}, "
                      f"в последнем — {cycle['last_p1_p2']}."]
            for review in cycle["reviews"]:
                if review.get("result"):
                    lines.append(f"- {review.get('directory')}: " + json.dumps(review["result"].get("findings", []), ensure_ascii=False))
            lines.append("")
        for session in sessions:
            lines += [f"### {STAGE_NAMES[session['stage']]}: {Path(session['directory']).relative_to(run_dir)}", "",
                      f"Статус: {session['status']}; ID: {session['session_id'] or 'неизвестен'}.",
                      f"Токены: {token_text(session['tokens'])}; входные: {session['tokens']['input_tokens']}; "
                      f"выходные: {session['tokens']['output_tokens']}; кешированные: {session['tokens']['cached_input_tokens']}."]
            for change in session["limit_usage"]:
                delta = change["consumed_percentage_points"]
                lines.append(f"- {change['limit_id']} / {change['window']}: {change['before_used_percent']}% → "
                             f"{change['after_used_percent']}%; " +
                             (f"расход {delta:g} п.п. лимита." if delta is not None else f"расход неизвестен: {change['reason']}"))
            if not session["limit_usage"]:
                lines.append("Лимит: неизвестно. " + session["limit_reason"])
            lines += [f"Журналы: [events.jsonl]({session['events_log']}), [stderr]({session['stderr_log']}).", ""]
            if session.get("error"):
                lines += ["Причина остановки: " + session["error"], ""]
        if report["last_error"]:
            lines += ["Причина остановки workflow: " + report["last_error"], ""]
        temporary = run_dir / "report.md.tmp"
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, run_dir / "report.md")
        state["usage"] = {"total": report["total"], "stages": report["stages"]}
    emit(kind="report", status="updated", report_path=str(run_dir / "report.md"), run_dir=str(run_dir))
    if announce:
        emit("Токены Codex: " + token_text(report["total"]["tokens"]))
        emit("Отчёт: " + str(run_dir / "report.md"))
    return report


def update_branch_review_report(state, run_dir):
    sessions = report_sessions(run_dir)
    report = {**state, "sessions": sessions, "total": aggregate_usage(sessions)}
    write_json(run_dir / "report.json", report)
    lines = [f"# Review: {state['branch']} → {state['base']}", "",
             f"Состояние: {state['phase']}", "Checks не запускались.", ""]
    for key in ("head_sha", "base_tip_sha", "base_sha"):
        lines.append(f"{key}: {state.get(key, 'не определён')}")
    result = state.get("review_result", {})
    lines += ["", result.get("summary", ""), ""]
    for item in result.get("findings", []):
        lines.append(f"- {item['priority']} {item['file']}:{item['line']}: {item['explanation']}")
    lines.extend(result.get("blockers", []))
    if state.get("last_error"):
        lines.append(state["last_error"])
    if state.get("retained_worktree"):
        lines.append("Сохранён worktree: " + state["worktree"])
    lines += ["", "Токенов: " + token_text(report["total"]["tokens"])]
    profile = state["execution_profile"]["review"]
    lines.append(f"Модель: {profile['model']}; глубина: {profile['effort']}")
    for session in sessions:
        lines.append(f"Сессия: {session['status']}; журналы: {session['directory']}")
        for change in session["limit_usage"]:
            lines.append(f"Лимит {change['limit_id']}: {change['consumed_percentage_points']} п.п.")
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    emit(kind="report", status="updated", report_path=str(run_dir / "report.md"), run_dir=str(run_dir))
    return report
