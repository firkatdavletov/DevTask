"""Review finding path validation and actionable diagnostics."""
import unittest

from dev_task.contracts import validate_review
from dev_task.errors import TaskError


class ReviewContractTest(unittest.TestCase):
    def result(self, file):
        return {"status": "reviewed", "summary": "Bug", "blockers": [],
                "findings": [{"priority": "P2", "file": file, "line": 104, "explanation": "Bug"}]}

    def test_worktree_relative_path_is_accepted(self):
        result = self.result("android/libs/example/File.kt")
        self.assertEqual(validate_review(result), result)

    def test_absolute_path_explains_required_path_base(self):
        with self.assertRaises(TaskError) as error:
            validate_review(self.result("/Users/example/worktrees/review/android/File.kt"))
        self.assertIn("/Users/example/worktrees/review/android/File.kt", str(error.exception))
        self.assertIn("абсолютный путь", str(error.exception))
        self.assertIn("относительно корня текущего worktree", str(error.exception))

    def test_invalid_paths_remain_rejected(self):
        for file in ("../File.kt", "android/../File.kt", "", None, 42):
            with self.subTest(file=file), self.assertRaises(TaskError):
                validate_review(self.result(file))
