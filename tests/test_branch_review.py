import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dev_task.branch_review import run_branch_review
from dev_task.config import load_config
from dev_task.errors import TaskError
from dev_task.repository import readonly_snapshot


class BranchReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'develop')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'Test')
        (self.repo / 'old').write_text('base\n')
        (self.repo / '.gitignore').write_text('build/\n')
        self.git('add', '.')
        self.git('commit', '-m', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        subprocess.run(['git', 'init', '--bare', str(self.root / 'remote')], check=True, capture_output=True)
        self.git('remote', 'add', 'origin', str(self.root / 'remote'))
        self.git('push', 'origin', 'develop')
        self.git('checkout', '-b', 'feature/test')
        (self.repo / 'old').unlink()
        (self.repo / 'new').write_text('new\n')
        self.git('add', '.')
        self.git('commit', '-m', 'change')
        self.git('push', 'origin', 'feature/test')
        (self.repo / 'new').write_text('staged\n')
        self.git('add', '.')
        (self.repo / 'new').write_text('dirty\n')
        (self.repo / 'untracked').write_text('keep')
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'repo': str(self.repo), 'worktrees_root': str(self.root / 'trees'),
                                          'runs_root': str(self.root / 'runs'), 'codex_bin': 'git'}))
        self.args = argparse.Namespace(config=self.config, branch='feature/test', base='develop')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def run_review(self, behavior=None):
        before = readonly_snapshot(self.repo)
        refs = self.git('show-ref')
        result = {'status': 'reviewed', 'summary': 'OK', 'findings': [], 'blockers': []}
        def fake(config, state, directory, stage, prompt, schema, validator, sandbox):
            self.assertEqual(sandbox, 'read-only')
            self.assertEqual(state['base_sha'], self.base)
            self.assertEqual(self.git('branch', '--show-current'), 'feature/test')
            self.assertIn('new', (directory.parents[1] / 'review.diff').read_text())
            if behavior:
                behavior(Path(state['worktree']))
            return getattr(self, 'review_result', result)
        try:
            with patch('dev_task.branch_review.launch_codex', side_effect=fake):
                run_branch_review(self.args, load_config(self.config, review_only=True))
        finally:
            self.assertEqual(readonly_snapshot(self.repo), before)
            self.assertEqual(self.git('show-ref'), refs)

    def test_review_preserves_dirty_checkout_and_removes_worktree(self):
        self.run_review()
        self.assertEqual(list((self.root / 'trees').iterdir()), [])
        report = next((self.root / 'runs').rglob('report.json'))
        self.assertEqual(json.loads(report.read_text())['review_result']['summary'], 'OK')

    def test_errors_cleanup_and_report(self):
        for error in (TaskError('failure'), KeyboardInterrupt(), subprocess.TimeoutExpired('codex', 1)):
            with self.subTest(error=type(error)):
                def fail(worktree):
                    raise error
                with self.assertRaises(type(error)):
                    self.run_review(fail)
                self.assertEqual(list((self.root / 'trees').iterdir()), [])

    def test_mutated_worktree_is_retained(self):
        def mutate(worktree):
            (worktree / 'new').write_text('changed')
        with self.assertRaises(TaskError):
            self.run_review(mutate)
        self.assertEqual(len(list((self.root / 'trees').iterdir())), 1)

    def test_empty_diff_never_launches_codex(self):
        self.args.base = 'feature/test'
        with patch('dev_task.branch_review.launch_codex') as launch:
            run_branch_review(self.args, load_config(self.config, review_only=True))
            launch.assert_not_called()
        self.assertFalse((self.root / 'trees').exists())

    def test_missing_branch_saves_failure_and_cleans_refs(self):
        self.args.branch = 'missing'
        with self.assertRaises(TaskError):
            self.run_review()
        self.assertTrue(list((self.root / 'runs').rglob('report.json')))

    def test_ignored_file_is_retained(self):
        def ignored(worktree):
            # Use per-worktree ignored build output via tracked ignore configuration.
            (worktree / 'build').mkdir()
            (worktree / 'build' / 'output').write_text('artifact')
        self.run_review(ignored)
        self.assertTrue(list((self.root / 'trees').iterdir()))

    def test_diverged_base_uses_merge_base(self):
        # Advance remote base from another checkout while primary stays dirty.
        other = self.root / 'other'
        self.git('worktree', 'add', str(other), 'develop')
        (other / 'base-only').write_text('unrelated')
        for argv in (['add', '.'], ['commit', '-m', 'base advance'], ['push', 'origin', 'develop']):
            subprocess.run(['git', *argv], cwd=other, check=True, capture_output=True)
        self.run_review()
        self.assertEqual((other / 'base-only').read_text(), 'unrelated')

    def test_real_launcher_profile_sandbox_and_invalid_json(self):
        cfg = json.loads(self.config.read_text())
        cfg.update(models={'custom': 'review-model'}, review_profile={'model': 'custom', 'effort': 'high'},
                   codex_args=['--add-dir', '/tmp'])
        self.config.write_text(json.dumps(cfg))
        def execute(argv, cwd, log, timeout, **kwargs):
            self.assertEqual(argv[argv.index('--model') + 1], 'review-model')
            self.assertEqual(argv[argv.index('--sandbox') + 1], 'read-only')
            self.assertIn('model_reasoning_effort=high', argv)
            self.assertNotIn('--add-dir', argv)
            Path(argv[argv.index('-o') + 1]).write_text('invalid json')
            return 0
        with patch('dev_task.codex.execute', side_effect=execute), patch('dev_task.sessions.subscription_snapshot', return_value={}):
            with self.assertRaises(ValueError):
                run_branch_review(self.args, load_config(self.config, review_only=True))
        self.assertEqual(list((self.root / 'trees').iterdir()), [])

    def test_blocking_findings_are_reported_without_repairs(self):
        self.review_result = {'status': 'reviewed', 'summary': 'Bug', 'blockers': [],
                              'findings': [{'priority': 'P1', 'file': 'new', 'line': 1, 'explanation': 'Bug'}]}
        with self.assertRaisesRegex(TaskError, 'замечания'):
            self.run_review()
        self.assertEqual(list((self.root / 'trees').iterdir()), [])
        report = json.loads(next((self.root / 'runs').rglob('report.json')).read_text())
        self.assertEqual(report['review_result']['findings'][0]['priority'], 'P1')
