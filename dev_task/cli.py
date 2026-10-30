"""Local Jira card -> isolated Codex worktree -> checked Draft PR. Python 3.10+."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("Установите зависимости: python3 -m pip install -r requirements.txt")

from .console import say
from .errors import TaskError, require
from .workflow import run_workflow


def parse_args(argv=None):
    # Читает аргументы запуска
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "publish", "report"))
    parser.add_argument("card", type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--local-only", action="store_true", help="Выполнить реализацию и проверки без commit/push/PR")
    parser.add_argument("--continue-pr", action="store_true", help="Начать доработку опубликованного Draft PR в той же ветке")
    parser.add_argument("--rerun", action="store_true", help="Повторно запустить Codex до создания коммита, сохранив diff")
    parser.add_argument("--skip-review", action="store_true", help="Пропустить review в текущем цикле run")
    parser.add_argument("--skip-planning", action="store_true", help="Пропустить planning в текущем цикле run")
    args = parser.parse_args(argv)
    require(args.command == "run" or not (args.local_only or args.rerun or args.skip_review or args.skip_planning or args.continue_pr),
            "Флаги --local-only/--rerun/--continue-pr/--skip-review/--skip-planning относятся к run.")
    require(not (args.continue_pr and args.rerun), "--continue-pr несовместим с --rerun.")
    return args


def main(argv=None):
    try:
        run_workflow(parse_args(argv))
        return 0
    except KeyboardInterrupt:
        say("Остановлено. Worktree и состояние сохранены.")
        return 130
    except (TaskError, OSError, ValueError, yaml.YAMLError, subprocess.SubprocessError) as exc:
        print("dev-task: " + str(exc), file=sys.stderr)
        return 1
