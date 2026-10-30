"""Workflow errors and invariant checks."""
from __future__ import annotations



class TaskError(Exception):
    pass


class CheckFailed(TaskError):
    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence


def require(condition, message):
    if not condition:
        raise TaskError(message)
