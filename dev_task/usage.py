"""Codex event usage and subscription-limit accounting."""
from __future__ import annotations

import contextlib
import json
import os
import selectors
import signal
import subprocess
import time


TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens")


STAGE_NAMES = {"planning": "Планирование", "coding": "Разработка", "check_fix": "Исправления checks",
               "review": "Проверка кода", "review_fix": "Исправления review",
               "commit_message": "Сообщение коммита"}


def event_usage(path, session_status):
    """Completed-turn usage is additive; cached input is already included in input."""
    values = {key: [] for key in TOKEN_FIELDS}
    started = completed = usage_turns = 0
    damaged = False
    session_id = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
        except ValueError:
            damaged = True
            continue
        kind = event.get("type")
        if kind == "thread.started":
            session_id = event.get("thread_id")
        elif kind == "turn.started":
            started += 1
        elif kind == "turn.failed":
            damaged = True
        elif kind == "turn.completed":
            completed += 1
            usage = event.get("usage")
            if not isinstance(usage, dict):
                damaged = True
                continue
            usage_turns += 1
            for key in TOKEN_FIELDS:
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    values[key].append(value)
                else:
                    damaged = True
    tokens = {key: sum(items) if items else None for key, items in values.items()}
    known = [tokens[key] for key in ("input_tokens", "output_tokens") if tokens[key] is not None]
    tokens["total_tokens"] = sum(known) if known else None
    complete = (completed > 0 and completed == usage_turns and started <= completed and not damaged
                and session_status not in ("running", "interrupted", "timed_out"))
    tokens["status"] = "complete" if complete else "partial" if known else "unknown"
    return tokens, session_id


def normalize_limits(result):
    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict):
        old = result.get("rateLimits")
        buckets = {old.get("limitId") or "codex": old} if isinstance(old, dict) else {}
    normalized = {}
    for limit_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        windows = {}
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if not isinstance(window, dict):
                continue
            percent, duration, reset = (window.get(k) for k in ("usedPercent", "windowDurationMins", "resetsAt"))
            if (type(percent) not in (int, float) or not 0 <= percent <= 100
                    or type(duration) is not int or duration <= 0 or type(reset) is not int or reset <= 0):
                continue
            windows[name] = {"usedPercent": percent, "windowDurationMins": duration, "resetsAt": reset}
        if windows:
            normalized[str(limit_id)] = windows
    return normalized


def subscription_snapshot(config, cwd, log):
    """Read account metadata only. Never create a thread or start a model turn."""
    snapshot = {"captured_at": time.time(), "status": "unavailable", "buckets": {}, "log": str(log)}
    process = None
    overrides = []
    args = iter(config["codex_args"])
    for arg in args:
        if arg.startswith("-p") or arg == "--profile" or arg.startswith("--profile="):
            snapshot["reason"] = "Профиль exec нельзя безопасно передать app-server этой версии."
            return snapshot
        if arg in ("-c", "--config", "--enable", "--disable"):
            value = next(args, None)
            if value is not None:
                overrides.extend((arg, value))
        elif arg.startswith(("--config=", "--enable=", "--disable=")) or arg.startswith("-c"):
            overrides.append(arg)
    try:
        with log.open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen([config["codex_bin"], "app-server", *overrides], cwd=cwd,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                       start_new_session=True)
            def send(message):
                process.stdin.write((json.dumps(message) + "\n").encode())
                process.stdin.flush()

            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "dev_task_usage", "version": "1.0"}}})
            deadline, buffer = time.monotonic() + 10, b""
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        snapshot["reason"] = "Таймаут чтения лимитов подписки (10 секунд)."
                        return snapshot
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        snapshot["reason"] = "app-server завершился без статистики лимитов."
                        return snapshot
                    buffer += chunk
                    if len(buffer) > 1024 * 1024:
                        snapshot["reason"] = "Некорректный ответ app-server: превышен размер сообщения."
                        return snapshot
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            message = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(message, dict) or message.get("id") not in (1, 2):
                            continue
                        if "error" in message:
                            snapshot["reason"] = "app-server не предоставил лимиты текущего аккаунта."
                            return snapshot
                        if message["id"] == 1:
                            send({"method": "initialized", "params": {}})
                            send({"id": 2, "method": "account/rateLimits/read", "params": {}})
                        else:
                            result = message.get("result")
                            snapshot["buckets"] = normalize_limits(result) if isinstance(result, dict) else {}
                            snapshot["status"] = "available" if snapshot["buckets"] else "unavailable"
                            if not snapshot["buckets"]:
                                snapshot["reason"] = "У аккаунта нет доступных показателей лимитов подписки."
                            return snapshot
    except (OSError, ValueError, subprocess.SubprocessError):
        snapshot["reason"] = "Не удалось прочитать лимиты через app-server."
        return snapshot
    finally:
        if process is not None:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            with contextlib.suppress(OSError):
                process.stdin.close()
            process.stdout.close()


def limit_changes(before, after):
    before, after = before.get("buckets", {}), after.get("buckets", {})
    changes = []
    for limit_id in sorted(before.keys() | after.keys()):
        for name in sorted(before.get(limit_id, {}).keys() | after.get(limit_id, {}).keys()):
            first, last = before.get(limit_id, {}).get(name), after.get(limit_id, {}).get(name)
            reference = first or last
            reason = None
            if first is None or last is None:
                reason = "Нет одного из снимков окна."
            elif (first["windowDurationMins"], first["resetsAt"]) != (last["windowDurationMins"], last["resetsAt"]):
                reason = "Окно лимита изменилось или сбросилось."
            elif last["usedPercent"] < first["usedPercent"]:
                reason = "Показатель расхода уменьшился."
            changes.append({"limit_id": limit_id, "window": name,
                            "window_duration_mins": reference["windowDurationMins"], "resets_at": reference["resetsAt"],
                            "before_used_percent": first["usedPercent"] if first else None,
                            "after_used_percent": last["usedPercent"] if last else None,
                            "consumed_percentage_points": None if reason else round(last["usedPercent"] - first["usedPercent"], 6),
                            "reason": reason})
    return changes


def aggregate_usage(sessions):
    tokens = {}
    for key in (*TOKEN_FIELDS, "total_tokens"):
        known = [s["tokens"][key] for s in sessions if s["tokens"][key] is not None]
        tokens[key] = sum(known) if known else None if sessions else 0
    tokens["status"] = ("complete" if all(s["tokens"]["status"] == "complete" for s in sessions)
                        else "unknown" if tokens["total_tokens"] is None else "partial")
    windows = {}
    for session in sessions:
        for change in session["limit_usage"]:
            key = (change["limit_id"], change["window"], change["window_duration_mins"], change["resets_at"])
            bucket = windows.setdefault(key, {k: change[k] for k in
                ("limit_id", "window", "window_duration_mins", "resets_at")})
            bucket.setdefault("known_percentage_points", None)
            bucket.setdefault("unknown_intervals", 0)
            if change["consumed_percentage_points"] is None:
                bucket["unknown_intervals"] += 1
            else:
                bucket["known_percentage_points"] = round((bucket["known_percentage_points"] or 0) + change["consumed_percentage_points"], 6)
    return {"session_count": len(sessions), "tokens": tokens, "limit_usage": list(windows.values()),
            "sessions_without_limit_data": sum(not s["limit_usage"] for s in sessions)}
