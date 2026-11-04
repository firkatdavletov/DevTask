"""Bounded subprocess execution and process-group cleanup."""
from __future__ import annotations

import contextlib
import os
import signal
import shutil
import subprocess
from pathlib import Path

from .errors import TaskError
from .events import process_environment


def capture(argv, cwd, *, ok=True):
    """Never execute card/config text through a shell."""
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=120, env=process_environment())
    if ok and result.returncode:
        # Do not print stderr: remote URLs / auth errors can contain credentials.
        raise TaskError(f"Команда {argv[0]} {argv[1] if len(argv) > 1 else ''} завершилась с кодом {result.returncode}.")
    return result


def execute(argv, cwd, log, timeout, *, input_text=None, stderr_log=None):
    """Bound runtime and terminate the whole process group, including Gradle children."""
    with contextlib.ExitStack() as stack:
        stdout = stack.enter_context(Path(log).open("w", encoding="utf-8"))
        stderr = stack.enter_context(Path(stderr_log).open("w", encoding="utf-8")) if stderr_log else subprocess.STDOUT
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, text=True, start_new_session=True, env=process_environment())
        try:
            process.communicate(input=input_text, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
        return process.returncode


def executable_path(name):
    env = process_environment()
    return shutil.which(name, path=env.get("PATH", os.defpath) if env is not None else None)
