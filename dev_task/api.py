"""Public synchronous orchestration API for CLI, TUI and integrations."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .branch_review import run_branch_review
from .config import load_config
from .errors import require
from .events import ExecutionContext, ProgressEvent, build_environment, emit
from .models import OperationResult, ProjectContext, ReviewRequest, RunRequest, TaskRequest, WorkflowRequest
from .processes import capture
from .storage import read_json
from .workflow import run_workflow


__all__ = ["Orchestrator", "load_project", "ProjectContext", "RunRequest", "TaskRequest",
           "ReviewRequest", "OperationResult", "ProgressEvent"]


def load_project(project: Path, config_path: Path | None = None, *, offline: bool = False) -> ProjectContext:
    """Resolve an explicit directory to its Git root. Does not change cwd."""
    directory = Path(project).expanduser().resolve()
    if offline:
        # Reports can outlive the checkout and do not require a working Git executable.
        root = (next((p for p in (directory, *directory.parents) if (p / ".git").exists()), directory)
                if directory.is_dir() else directory)
    else:
        require(directory.is_dir(), "Каталог проекта не найден: " + str(directory))
        root = Path(capture(["git", "rev-parse", "--show-toplevel"], directory).stdout.strip()).resolve()
    path = Path(config_path).expanduser().resolve() if config_path is not None else root / ".dev-task/config.json"
    require(path.is_file(), "Конфигурация не найдена: " + str(path) + ". Создайте файл или передайте --config.")
    return ProjectContext(root, path)


class Orchestrator:
    """One operation per call; callbacks run synchronously in the calling context.

    Observer exceptions propagate after normal workflow cleanup. With no observer,
    operations never print. Execution environment and observer are context-local.
    Expected workflow failures raise TaskError; I/O errors retain their native type.
    """

    def __init__(self, project: ProjectContext, *, on_event: Callable[[ProgressEvent], None] | None = None):
        self.project = project
        self.on_event = on_event

    def run(self, request: RunRequest) -> OperationResult:
        require(isinstance(request, RunRequest), "run требует RunRequest.")
        require(not (request.continue_pr and request.rerun), "--continue-pr несовместим с --rerun.")
        return self._execute("run", WorkflowRequest(**asdict(request)))

    def publish(self, request: TaskRequest) -> OperationResult:
        require(type(request) is TaskRequest, "publish требует TaskRequest без флагов run.")
        return self._execute("publish", WorkflowRequest(card=request.card, command="publish"))

    def report(self, request: TaskRequest) -> OperationResult:
        require(type(request) is TaskRequest, "report требует TaskRequest без флагов run.")
        return self._execute("report", WorkflowRequest(card=request.card, command="report"))

    def review(self, request: ReviewRequest) -> OperationResult:
        require(isinstance(request, ReviewRequest) and request.branch and request.base,
                "review требует ReviewRequest с branch и base.")
        return self._execute("review", request)

    def _execute(self, operation, request):
        with ExecutionContext(operation, self.on_event).activate():
            try:
                raw = read_json(self.project.config_path)
                if isinstance(raw, dict) and "repo" in raw:
                    emit("config.repo устарел и игнорируется; используйте cwd или --project.",
                         kind="warning", field="repo")
                config = load_config(self.project.config_path, project=self.project.root,
                                     review_only=operation == "review")
                for key in ("runs_root", "worktrees_root"):
                    require(not config[key].is_relative_to(self.project.root), key + " должен находиться вне checkout.")
                require(not config["runs_root"].is_relative_to(config["worktrees_root"]) and
                        not config["worktrees_root"].is_relative_to(config["runs_root"]),
                        "Каталоги runs и worktrees должны быть раздельными.")
                with ExecutionContext(operation, self.on_event, build_environment(config)).activate():
                    emit(kind="operation", status="started")
                    result = run_branch_review(request, config) if operation == "review" else run_workflow(request, config)
                    emit(kind="operation", status="completed", run_dir=str(result.run_dir),
                         worktree=str(result.worktree) if result.worktree else None, pr=result.pr)
                    return result
            except BaseException as exc:
                emit(kind="operation", status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                     error=str(exc), error_type=type(exc).__name__)
                raise
