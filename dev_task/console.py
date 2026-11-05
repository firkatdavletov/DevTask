"""Thread-safe terminal progress; plain history when redirected."""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time


def say(message):
    print(message, flush=True)


class ProgressRenderer:
    def __init__(self, stream=None, *, clock=time.monotonic):
        self.stream = stream if stream is not None else sys.stdout
        self.clock = clock
        self.animated = self.stream.isatty() and not os.environ.get("CI") and os.environ.get("TERM") != "dumb"
        self.active = {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.frame = 0

    def start(self):
        if self.animated:
            self.thread = threading.Thread(target=self._animate, daemon=True)
            self.thread.start()

    def _animate(self):
        while not self.stop.wait(0.1):
            self.tick()

    def _clear(self):
        if self.animated:
            self.stream.write("\r\033[2K")

    def _write(self, text):
        encoding = self.stream.encoding or "utf-8"
        self.stream.write(text.encode(encoding, errors="replace").decode(encoding))
        self.stream.flush()

    def message(self, text):
        with self.lock:
            self._clear()
            self._write(text + "\n")
            self.tick()

    def tick(self):
        with self.lock:
            if not self.animated or not self.active:
                return
            name, started = next(reversed(self.active.values()))
            seconds = int(self.clock() - started)
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            try:
                frames.encode(self.stream.encoding or "utf-8")
            except UnicodeEncodeError:
                frames = "|/-\\"
            text = f"{frames[self.frame % len(frames)]} {name} · выполняется {seconds // 60:02}:{seconds % 60:02}"
            self.frame += 1
            self._clear()
            self._write(text[:max(1, shutil.get_terminal_size().columns - 1)])

    def __call__(self, event):
        with self.lock:
            details = event.details
            key = None
            labels = {"planning": "Планирование", "coding": "Разработка", "review": "Review",
                      "check_fix": "Исправление проверок", "review_fix": "Исправление review",
                      "setup": "Подготовка окружения", "check": "Проверка"}
            name = details.get("name") or labels.get(event.stage, event.stage) or "Операция"
            status = event.status
            if event.kind == "step":
                key = details["step_id"]
            elif event.kind == "check":
                key = ("check", event.stage, name)
            if key is not None:
                if status == "started":
                    self.active[key] = (name, self.clock())
                    if not self.animated:
                        self.message("[Начато] " + name)
                    self.tick()
                else:
                    _, started = self.active.pop(key, (name, self.clock()))
                    label = {"completed": "Готово", "passed": "Готово", "failed": "Ошибка", "interrupted": "Прервано"}.get(status, status)
                    self.message(f"[{label}] {name} — {self.clock() - started:.1f} с")
                return
            if event.kind == "stage" and status in ("skipped", "reused"):
                self.message(f"[{'Пропущено' if status == 'skipped' else 'Использован результат'}] {name}: " + str(details.get("reason") or details.get("plan_path") or "результат сохранён"))
            elif event.kind == "retry":
                self.message(f"Повтор {name}: {details.get('iteration')}/{details.get('limit')}")
            elif event.message:
                self.message(event.message)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        with self.lock:
            self._clear()
            self.stream.flush()
            self.active.clear()
