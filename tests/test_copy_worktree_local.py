"""Personal skill selection and conflicts using real Git worktrees."""
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "dev_task/copy-worktree-local.sh"


class CopyWorktreeLocalTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "main repo"
        self.worktree = self.root / "task worktree"
        self.repo.mkdir()
        self.git(self.repo, "init")
        self.git(self.repo, "config", "user.name", "Test")
        self.git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("Test repository\n")
        self.skill(self.repo, "tracked", "tracked original")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-m", "Initial")
        self.git(self.repo, "worktree", "add", "--detach", str(self.worktree))

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              text=True, capture_output=True).stdout

    def skill(self, repo, name, content):
        path = repo / ".agents/skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def exclude(self, patterns):
        with (self.repo / ".git/info/exclude").open("a") as stream:
            stream.write("\n" + patterns + "\n")

    def copy(self):
        return subprocess.run(["bash", str(SCRIPT), str(self.repo), str(self.worktree)],
                              check=True, text=True, capture_output=True).stdout

    def test_only_ignored_skills_with_skill_md_are_copied(self):
        self.exclude("/.agents/skills/personal skill/\n/.agents/skills/tracked/\n/.agents/skills/incomplete/")
        personal = self.skill(self.repo, "personal skill", "personal")
        helper = personal.parent / ".hidden/helper.sh"
        helper.parent.mkdir()
        helper.write_text("#!/bin/sh\n")
        helper.chmod(0o755)
        self.skill(self.repo, "not ignored", "untracked")
        (self.repo / ".agents/skills/tracked/SKILL.md").write_text("local tracked edit")
        (self.repo / ".agents/skills/incomplete").mkdir()

        self.copy()

        self.assertEqual((self.worktree / personal.relative_to(self.repo)).read_text(), "personal")
        copied_helper = self.worktree / helper.relative_to(self.repo)
        self.assertEqual(copied_helper.read_text(), helper.read_text())
        self.assertTrue(copied_helper.stat().st_mode & 0o111)
        self.assertFalse((self.worktree / ".agents/skills/not ignored").exists())
        self.assertFalse((self.worktree / ".agents/skills/incomplete").exists())
        self.assertEqual((self.worktree / ".agents/skills/tracked/SKILL.md").read_text(), "tracked original")

    def test_tracked_skill_in_target_is_preserved(self):
        self.git(self.repo, "rm", "--cached", ".agents/skills/tracked/SKILL.md")
        self.exclude("/.agents/skills/tracked/")
        self.skill(self.repo, "tracked", "personal replacement")

        output = self.copy()

        self.assertIn("Skip conflict: tracked", output)
        self.assertEqual((self.worktree / ".agents/skills/tracked/SKILL.md").read_text(), "tracked original")

    def test_missing_skills_directory_is_skipped(self):
        self.git(self.repo, "rm", "-r", ".agents")
        self.assertEqual(self.copy(), "")

    def test_non_ignored_skill_in_target_is_preserved(self):
        (self.repo / ".gitignore").write_text("/.agents/skills/personal/\n")
        self.skill(self.repo, "personal", "source")
        target = self.skill(self.worktree, "personal", "target")

        output = self.copy()

        self.assertIn("Skip conflict: personal", output)
        self.assertEqual(target.read_text(), "target")


if __name__ == "__main__":
    unittest.main()
