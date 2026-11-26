"""Compare CLI behavior using independent local repositories and fake services."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import test_workflow as workflow


PYTHON_CLI = (sys.executable, str(workflow.SCRIPT))
REPORT_ARGS = ("report", "task.md", "--project", "repo", "--config", "config.json")
PROGRESS_DURATION = re.compile(r"(?m)^(\[(?:Готово|Ошибка|Прервано)\] [^\n]+) — \d+\.\d+ с$".encode())


def observe(executable, args, prepare=None):
    with tempfile.TemporaryDirectory(prefix="devtask-diff-") as directory:
        root = Path(directory).resolve()
        repo = root / "repo"
        remote = root / "remote.git"
        bin_path = root / "bin"
        workflow.install_fake_commands(bin_path)
        env = {
            "HOME": str(root),
            "PATH": str(bin_path) + os.pathsep + os.environ["PATH"],
            "TEST_ROOT": str(root),
            "REAL_GIT": workflow.REAL_GIT,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z",
            "PYTHONUTF8": "1",
        }

        def git(cwd, *argv):
            return subprocess.run([workflow.REAL_GIT, *argv], cwd=cwd, env=env,
                                  capture_output=True, check=True, timeout=30).stdout

        git(root, "init", "--bare", str(remote))
        git(root, "init", "-b", "develop", str(repo))
        (repo / "name.txt").write_text("original\n", encoding="utf-8")
        git(repo, "add", "name.txt")
        git(repo, "config", "user.name", "Test Developer")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "commit", "-m", "Initial")
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "push", "origin", "develop")
        repo_key = hashlib.sha256(os.fsencode(repo)).hexdigest()[:12]
        if prepare:
            prepare(root, repo_key)

        def normalize(value):
            return value.replace(os.fsencode(root), b"<fixture>").replace(
                f"/{repo_key}/".encode(), b"/<repo-key>/")

        result = subprocess.run([*executable, *args], cwd=root, env=env, capture_output=True, timeout=30)
        observation = {
            "exit code": result.returncode,
            "stdout": PROGRESS_DURATION.sub(rb"\1 <elapsed>", normalize(result.stdout)),
            "stderr": normalize(result.stderr),
            "checkout status": git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
            "local refs": git(repo, "for-each-ref"),
            "remote refs": git(remote, "for-each-ref"),
        }
        for path in (root / "runs").rglob("*"):
            if path.is_file():
                name = normalize(b"/" + os.fsencode(path.relative_to(root)))[1:].decode()
                observation[name] = normalize(path.read_bytes())
        for name in ("calls", "codex-calls.jsonl", "github-prs.json"):
            path = root / name
            if path.is_file():
                observation[name] = normalize(path.read_bytes())
        return observation


def differences(reference, candidate):
    return {name: (reference.get(name), candidate.get(name))
            for name in sorted(reference.keys() | candidate.keys())
            if reference.get(name) != candidate.get(name)}


def seed_report(root, repo_key):
    (root / "config.json").write_text(json.dumps({
        "github_repo": "github.test/team/app",
        "runs_root": "runs",
        "worktrees_root": "worktrees",
        "checks": [{"name": "unused", "argv": ["false"]}],
    }), encoding="utf-8")
    (root / "task.md").write_text(
        "---\nissue: DEMO-1\nbase: develop\ntitle: Report fixture\ncomplexity: trivial\n---\nRead the report.\n",
        encoding="utf-8",
    )
    run = root / "runs" / repo_key / "DEMO-1"
    run.mkdir(parents=True)
    (run / "state.json").write_text('{"issue": "DEMO-1", "phase": "verified"}\n', encoding="utf-8")


class DifferentialTest(unittest.TestCase):
    def test_equivalent_report_runs_match(self):
        reference = observe(PYTHON_CLI, REPORT_ARGS, seed_report)
        candidate = observe(PYTHON_CLI, REPORT_ARGS, seed_report)
        self.assertEqual(reference["exit code"], 0, reference["stderr"])
        self.assertIn("runs/<repo-key>/DEMO-1/report.md", reference)
        self.assertNotIn("calls", reference)
        self.assertEqual(differences(reference, candidate), {})

    def test_detects_exit_and_artifact_changes(self):
        wrapper = """
import subprocess
import sys
from pathlib import Path

subprocess.run(sys.argv[1:], check=True)
report, = Path("runs").glob("*/*/report.md")
with report.open("ab") as stream:
    stream.write(b"changed report")
sys.exit(42)
"""
        reference = observe(PYTHON_CLI, REPORT_ARGS, seed_report)
        changed = observe((sys.executable, "-c", wrapper, *PYTHON_CLI), REPORT_ARGS, seed_report)
        diff = differences(reference, changed)
        self.assertEqual(set(diff), {"exit code", "runs/<repo-key>/DEMO-1/report.md"})
        self.assertEqual(diff["exit code"], (0, 42))

    @unittest.skipUnless(os.environ.get("DEV_TASK_GO_BINARY"), "set DEV_TASK_GO_BINARY to test the Go preview")
    def test_go_preview_runs_against_same_fixture(self):
        binary = Path(os.environ["DEV_TASK_GO_BINARY"]).resolve(strict=True)
        reference = observe(PYTHON_CLI, ())
        candidate = observe((str(binary),), ())
        self.assertEqual(reference["exit code"], 2, reference["stderr"])
        # GO-103 takes over help and argument diagnostics.
        self.assertEqual(set(differences(reference, candidate)), {"stderr"})
        self.assertEqual(candidate["stderr"], b"dev-task: a command is required\n")
