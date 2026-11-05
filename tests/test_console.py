import io
import os
import unittest
from unittest.mock import patch

from dev_task.console import ProgressRenderer
from dev_task.events import ExecutionContext, ProgressEvent, step


class Terminal(io.StringIO):
    def isatty(self):
        return True


class ConsoleTest(unittest.TestCase):
    def test_nested_steps_pair_on_failure(self):
        events = []
        with ExecutionContext("run", events.append).activate():
            with step("parent"):
                with self.assertRaises(ValueError):
                    with step("child"):
                        raise ValueError()
        self.assertEqual([e.status for e in events], ["started", "started", "failed", "completed"])
        self.assertEqual(events[1].details["parent_id"], events[0].details["step_id"])

    def test_plain_history_and_interruption(self):
        stream = io.StringIO()
        now = [0]
        renderer = ProgressRenderer(stream, clock=lambda: now[0])
        with self.assertRaises(KeyboardInterrupt), ExecutionContext("run", renderer).activate():
            with step("fetch"):
                now[0] = 2
                raise KeyboardInterrupt()
        renderer.close()
        self.assertEqual(stream.getvalue(), "[Начато] fetch\n[Прервано] fetch — 2.0 с\n")
        self.assertNotIn("\033", stream.getvalue())

    def test_animation_returns_to_parent_and_thread_stops(self):
        stream = Terminal()
        with patch.dict(os.environ, {"TERM": "xterm", "CI": ""}):
            renderer = ProgressRenderer(stream, clock=lambda: 18)
        with ExecutionContext("run", renderer).activate():
            with step("parent"):
                with step("child"):
                    renderer.tick()
                    self.assertIn("child", stream.getvalue())
                renderer.tick()
                self.assertIn("parent", stream.getvalue().split("\r")[-1])
                renderer.message("log")
        renderer.start()
        renderer.close()
        self.assertFalse(renderer.thread.is_alive())
        self.assertFalse(renderer.active)

    def test_ci_disables_animation(self):
        with patch.dict(os.environ, {"CI": "true", "TERM": "xterm"}):
            renderer = ProgressRenderer(Terminal())
        self.assertFalse(renderer.animated)

    def test_narrow_terminal_and_ascii_spinner(self):
        class AsciiTerminal(Terminal):
            @property
            def encoding(self):
                return "ascii"
        stream = AsciiTerminal()
        with patch.dict(os.environ, {"TERM": "xterm", "CI": ""}), patch(
                "dev_task.console.shutil.get_terminal_size", return_value=os.terminal_size((20, 24))):
            renderer = ProgressRenderer(stream, clock=lambda: 1)
            with ExecutionContext("run", renderer).activate(), step("long operation name"):
                renderer.tick()
                frame = stream.getvalue().split("\033[2K")[-1]
                self.assertLessEqual(len(frame), 19)
                self.assertIn(frame[0], "|/-\\")
        renderer.close()

    def test_failed_check_keeps_parent_active_for_repair(self):
        renderer = ProgressRenderer(io.StringIO())
        with ExecutionContext("run", renderer).activate(), step("workflow"):
            renderer(ProgressEvent("check", stage="check", status="started", details={"name": "build"}))
            renderer(ProgressEvent("check", stage="check", status="failed", details={"name": "build"}))
            renderer(ProgressEvent("retry", stage="check_fix", details={"iteration": 1, "limit": 2}))
            self.assertEqual(len(renderer.active), 1)
            self.assertIn("1/2", renderer.stream.getvalue())
        renderer.close()
