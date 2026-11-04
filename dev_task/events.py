"""Execution-local progress and environment; no terminal dependencies."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
from typing import Callable, Mapping


@dataclass(frozen=True)
class ProgressEvent:
    kind: str
    operation: str = ""
    stage: str | None = None
    status: str | None = None
    message: str = ""
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionContext:
    operation: str
    on_event: Callable[[ProgressEvent], None] | None = None
    env: Mapping[str, str] | None = None

    @contextmanager
    def activate(self):
        token = _current.set(self)
        try:
            yield
        finally:
            _current.reset(token)


_current: ContextVar[ExecutionContext | None] = ContextVar("dev_task_execution", default=None)


def emit(message="", *, kind="progress", stage=None, status=None, **details):
    context = _current.get()
    if context is not None and context.on_event is not None:
        context.on_event(ProgressEvent(kind, context.operation, stage, status, message, details))


def process_environment():
    context = _current.get()
    return dict(context.env) if context is not None and context.env is not None else None


def build_environment(config):
    env = dict(os.environ)
    if "android_home" in config:
        env.setdefault("ANDROID_HOME", str(config["android_home"]))
    env.update(config.get("env", {}))
    return env
