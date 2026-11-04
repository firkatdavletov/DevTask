"""Interface-independent requests, project context and operation results."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ProjectContext:
    root: Path
    config_path: Path


@dataclass(frozen=True)
class TaskRequest:
    card: Path


@dataclass(frozen=True)
class RunRequest(TaskRequest):
    local_only: bool = False
    continue_pr: bool = False
    rerun: bool = False
    skip_review: bool = False
    skip_planning: bool = False


@dataclass(frozen=True)
class ReviewRequest:
    branch: str
    base: str


@dataclass(frozen=True)
class WorkflowRequest(RunRequest):
    command: str = "run"


@dataclass(frozen=True)
class OperationResult:
    status: str
    run_dir: Path
    worktree: Path | None = None
    pr: Mapping[str, object] | None = None

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.json"

    @property
    def report_path(self) -> Path:
        return self.run_dir / "report.md"
