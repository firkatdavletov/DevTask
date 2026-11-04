"""Session lifecycle, usage persistence and report refresh."""
from __future__ import annotations

import contextlib
import subprocess
import time

from .errors import require
from .events import emit
from .reports import update_reports
from .storage import write_json
from .usage import event_usage, subscription_snapshot


@contextlib.contextmanager
def tracked_session(config, state, directory, worktree, stage):
    attempt = directory
    while not attempt.name.startswith("attempt-"):
        require(attempt != attempt.parent, "Каталог сессии должен находиться внутри attempt-*.")
        attempt = attempt.parent
    run_dir = attempt.parent
    record = {"stage": stage, "directory": str(directory), "status": "running", "recovered": False,
              "started_at": time.time()}
    usage_path = directory / "usage.json"
    write_json(usage_path, record)
    try:
        record["limits_before"] = subscription_snapshot(config, worktree, directory / "limits-before.stderr.log")
        write_json(usage_path, record)
        yield record
    except BaseException as exc:
        if record["status"] not in ("blocked", "needs_input"):
            record["status"] = ("interrupted" if isinstance(exc, KeyboardInterrupt) else
                                "timed_out" if isinstance(exc, subprocess.TimeoutExpired) else "failed")
        record["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        record["finished_at"] = time.time()
        record["limits_after"] = subscription_snapshot(config, worktree, directory / "limits-after.stderr.log")
        record["tokens"], record["session_id"] = event_usage(directory / "events.jsonl", record["status"])
        write_json(usage_path, record)
        update_reports(state, run_dir)
        emit(kind="agent", stage=stage, status=record["status"],
             directory=str(directory), session_id=record.get("session_id"), tokens=record["tokens"])
