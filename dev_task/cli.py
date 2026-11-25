"""Project-independent Codex task orchestration. Python 3.10+."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("Установите зависимости: python3 -m pip install -r requirements.txt")

from .console import say, ProgressRenderer
from .events import step, ExecutionContext
from .errors import TaskError, require
from .api import Orchestrator, load_project
from .models import ISSUE_PATTERN, ReviewRequest, RunRequest, TaskRequest


def parse_args(argv=None):
    # Читает аргументы запуска
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "publish", "report", "review"))
    parser.add_argument("card", help="Путь к карточке задачи, идентификатор для run (поиск в tasks_root) или удалённая ветка для review")
    parser.add_argument("--base", help="Базовая удалённая ветка для review")
    parser.add_argument("--config", type=Path, help="JSON конфигурация (по умолчанию .dev-task/config.json проекта)")
    parser.add_argument("--project", type=Path, help="Каталог проекта (по умолчанию cwd)")
    parser.add_argument("--local-only", action="store_true", help="Выполнить реализацию и проверки без commit/push/PR")
    parser.add_argument("--continue-pr", action="store_true", help="Начать доработку опубликованного Draft PR в той же ветке")
    parser.add_argument("--rerun", action="store_true", help="Повторно запустить Codex до создания коммита, сохранив diff")
    parser.add_argument("--skip-review", action="store_true", help="Пропустить review в текущем цикле run")
    parser.add_argument("--skip-planning", action="store_true", help="Пропустить planning в текущем цикле run")
    args = parser.parse_args(argv)
    if args.command != "review" and not (args.command == "run" and ISSUE_PATTERN.fullmatch(args.card)):
        args.card = Path(args.card)
    if args.command == "review" and not args.base:
        parser.error("review требует --base")
    if args.command != "review" and args.base:
        parser.error("--base относится к review")
    if args.command == "review" and (args.local_only or args.rerun or args.skip_review or args.skip_planning or args.continue_pr):
        parser.error("Флаги run несовместимы с review")
    require(args.command == "run" or not (args.local_only or args.rerun or args.skip_review or args.skip_planning or args.continue_pr),
            "Флаги --local-only/--rerun/--continue-pr/--skip-review/--skip-planning относятся к run.")
    require(not (args.continue_pr and args.rerun), "--continue-pr несовместим с --rerun.")
    return args


def main(argv=None):
    args = None
    report_path = None
    renderer = None

    def on_event(event):
        nonlocal report_path
        if event.kind == "report" and event.status == "updated":
            report_path = Path(event.details["report_path"])
        renderer(event)

    try:
        args = parse_args(argv)
        renderer = ProgressRenderer()
        renderer.start()
        with ExecutionContext(args.command, on_event).activate(), step("Загрузка проекта"):
            project = load_project(args.project if args.project is not None else Path.cwd(), args.config, offline=args.command == "report")
        orchestrator = Orchestrator(project, on_event=on_event)
        if args.command == "review":
            result = orchestrator.review(ReviewRequest(str(args.card), args.base))
        elif args.command == "run":
            result = orchestrator.run(RunRequest(args.card, local_only=args.local_only, continue_pr=args.continue_pr,
                                        rerun=args.rerun, skip_review=args.skip_review, skip_planning=args.skip_planning))
        else:
            result = getattr(orchestrator, args.command)(TaskRequest(args.card))
        report_path = result.report_path
        return 0
    except KeyboardInterrupt:
        if renderer:
            renderer.close()
        say("Остановлено. Результаты review сохранены." if args and args.command == "review"
            else "Остановлено. Worktree и состояние сохранены.")
        return 130
    except (TaskError, OSError, ValueError, yaml.YAMLError, subprocess.SubprocessError) as exc:
        if renderer:
            renderer.close()
        print("dev-task: " + str(exc), file=sys.stderr)
        return 1
    finally:
        if renderer:
            renderer.close()
        if report_path is not None:
            try:
                with report_path.open(encoding="utf-8", newline="") as report:
                    content = report.read()
            except (OSError, UnicodeError) as exc:
                print("dev-task: Не удалось прочитать отчёт " + str(report_path) + ": " + str(exc), file=sys.stderr)
            else:
                print(content, end="", flush=True)
