"""Continuation lifecycle with real local Git and fake model/GitHub processes."""
import json
from pathlib import Path
import sys
import unittest

import test_commit_messages as messages
import test_workflow as workflow


class ContinuePrTest(unittest.TestCase):
    g = workflow.WorkflowTest.g
    write_config = workflow.WorkflowTest.write_config
    write_card = workflow.WorkflowTest.write_card
    run_task = workflow.WorkflowTest.run_task
    calls = workflow.WorkflowTest.calls
    state = messages.CommitMessageTest.state
    commit_calls = messages.CommitMessageTest.commit_calls
    failing_hook = messages.CommitMessageTest.failing_hook

    def setUp(self):
        messages.CommitMessageTest.setUp(self)
        fake = workflow.FAKE_CODEX.replace(
            '        Path("app/name.txt").write_text("new name\\n")',
            '        Path("app/name.txt").write_text(os.environ.get("NAME_CONTENT", "new name\\n"))\n'
            '        if "EXTRA_CONTENT" in os.environ:\n'
            '            Path("app/extra.txt").write_text(os.environ["EXTRA_CONTENT"])')
        assert fake != workflow.FAKE_CODEX
        (self.bin / 'codex').write_text('#!' + sys.executable + '\n' + messages.COMMIT_CODEX + fake)
        self.run_task()
        self.initial = self.state()
        self.worktree = Path(self.initial['worktree'])
        self.state_file = next((self.root / 'runs').glob('*/*/state.json'))

    def change(self, content='Second revision'):
        self.card.write_text(self.card.read_text() + '\n' + content)
        self.env['EXTRA_CONTENT'] = content

    def prs(self):
        return json.loads((self.root / 'github-prs.json').read_text())

    def test_new_cycle_local_only_publish_and_multiple_cycles(self):
        prs = self.prs()
        prs[0].update(title='Manual title', body='Manual body')
        (self.root / 'github-prs.json').write_text(json.dumps(prs))
        (self.worktree / 'app/manual.txt').write_text('Manual edit')
        self.change()
        self.run_task('run', '--continue-pr', '--local-only')
        pending = self.state()
        self.assertEqual(pending['workflow_version'], 4)
        self.assertEqual(pending['cycle_base_sha'], self.initial['commit_sha'])
        self.assertEqual(pending['base_sha'], self.initial['base_sha'])
        self.assertNotIn('commit_sha', pending)
        self.assertEqual(len(pending['cycle_history']), 1)
        self.assertIn('Implement name editing', pending['previous_card_text'])
        self.run_task('publish')
        second = self.state()['commit_sha']
        self.assertEqual(self.g(self.worktree, 'rev-parse', second + '^'), self.initial['commit_sha'])
        self.assertEqual(self.prs(), prs)
        self.assertEqual((self.worktree / 'app/manual.txt').read_text(), 'Manual edit')
        self.change('Third revision')
        self.run_task('run', '--continue-pr')
        self.assertEqual(self.g(self.worktree, 'rev-parse', 'HEAD^'), second)
        self.assertEqual(len(self.state()['cycle_history']), 2)
        self.assertEqual(self.prs(), prs)

    def test_no_changes_and_v3_migration(self):
        state = self.state()
        state['workflow_version'] = 3
        for key in ('cycle_base_sha', 'last_published_sha'):
            state.pop(key)
        self.state_file.write_text(json.dumps(state))
        calls = len(self.calls())
        self.run_task()
        self.assertEqual(len(self.calls()), calls)
        self.assertEqual(self.state()['last_published_sha'], self.initial['commit_sha'])
        self.card.write_text(self.card.read_text() + '\nRecheck existing implementation.')
        result = self.run_task('run', '--continue-pr')
        self.assertIn('Изменений нет', result.stdout)
        self.assertEqual(self.state()['cycle_outcome'], 'no_changes')
        self.assertEqual(self.state()['commit_sha'], self.initial['commit_sha'])
        self.assertEqual(len(self.commit_calls()), 1)

    def test_failed_coding_and_card_change_require_rerun(self):
        self.change()
        self.env['FAIL_STAGE'] = 'coding'
        self.run_task('run', '--continue-pr', success=False)
        cycle = self.state()['cycle_id']
        self.card.write_text(self.card.read_text() + '\nMore requirements')
        self.assertIn('--rerun', self.run_task('run', '--continue-pr', success=False).stderr)
        self.env.pop('FAIL_STAGE')
        self.run_task('run', '--rerun', '--local-only')
        self.assertNotEqual(self.state()['cycle_id'], cycle)
        self.run_task('publish')
        self.assertEqual(self.g(self.worktree, 'rev-parse', 'HEAD^'), self.initial['commit_sha'])

    def test_review_resume_and_complete_diff(self):
        self.change()
        self.write_card(issue=self.initial['issue'], complexity='simple')
        self.env['FAIL_STAGE'] = 'review'
        self.run_task('run', '--continue-pr', success=False)
        cycle = self.state()['cycle_id']
        coding_calls = len([c for c in self.calls() if c['stage'] == 'coding'])
        self.env.pop('FAIL_STAGE')
        self.run_task('run', '--continue-pr', '--local-only')
        self.assertEqual(self.state()['cycle_id'], cycle)
        self.assertEqual(len([c for c in self.calls() if c['stage'] == 'coding']), coding_calls)
        diff = next(Path(self.state()['last_attempt']).glob('review-*/checked.diff')).read_text()
        self.assertIn('app/name.txt', diff)
        self.assertIn('app/extra.txt', diff)
        self.run_task('publish')
        delta = sorted(self.state_file.parent.glob('attempt-*/commit-message-*/checked.diff'), key=lambda p: p.stat().st_mtime)[-1].read_text()
        self.assertIn('app/extra.txt', delta)
        self.assertNotIn('app/name.txt', delta)

    def test_failed_hook_reuses_message_and_commit_push_retry(self):
        hook = self.failing_hook()
        self.change()
        self.run_task('run', '--continue-pr', success=False)
        self.assertNotIn('commit_sha', self.state())
        hook.write_text('#!/bin/sh\nexit 0\n')
        receive = self.remote / 'hooks/pre-receive'
        receive.write_text('#!/bin/sh\nexit 1\n')
        receive.chmod(0o755)
        self.run_task('publish', success=False)
        commit = self.state()['commit_sha']
        receive.write_text('#!/bin/sh\nexit 0\n')
        self.run_task('publish')
        self.assertEqual(self.state()['commit_sha'], commit)
        self.assertEqual(len(self.commit_calls()), 2)

    def test_lost_push_response_reuses_commit(self):
        wrapper = self.bin / 'git'
        original = wrapper.read_text()
        wrapper.write_text(original.replace('else:\n    os.execv', '''elif sys.argv[1:2] == ["push"] and os.environ.get("LOSE_PUSH"):
    import subprocess
    code = subprocess.run([os.environ["REAL_GIT"], *sys.argv[1:]]).returncode
    sys.exit(1 if code == 0 else code)
else:
    os.execv'''))
        self.change()
        self.env['LOSE_PUSH'] = '1'
        self.run_task('run', '--continue-pr', success=False)
        commit = self.state()['commit_sha']
        self.assertEqual(self.g(self.root, '--git-dir', str(self.remote), 'rev-parse', self.initial['branch']), commit)
        self.env.pop('LOSE_PUSH')
        self.run_task('publish')
        self.assertEqual(self.state()['commit_sha'], commit)
        self.assertEqual(len(self.prs()), 1)
        self.assertEqual(len(self.commit_calls()), 2)

    def test_reject_pr_changes_without_starting_cycle(self):
        original = self.prs()
        for change in (None, {'isDraft': False}, {'state': 'CLOSED'}, {'state': 'MERGED'}, {'number': 99}, {'baseRefName': 'other'}):
            with self.subTest(change=change):
                prs = [] if change is None else [{**original[0], **change}]
                (self.root / 'github-prs.json').write_text(json.dumps(prs))
                self.run_task('run', '--continue-pr', success=False)
                self.assertEqual(self.state()['cycle_id'], self.initial['cycle_id'])
                self.assertEqual(len(self.calls()), 1)

    def test_reject_foreign_head_and_changed_remote(self):
        self.g(self.worktree, 'commit', '--allow-empty', '-m', 'External')
        self.assertIn('HEAD', self.run_task('run', '--continue-pr', success=False).stderr)
        self.g(self.worktree, 'reset', '--hard', self.initial['commit_sha'])
        self.g(self.root, '--git-dir', str(self.remote), 'update-ref', 'refs/heads/' + self.initial['branch'], self.initial['base_sha'])
        self.assertIn('Удалённая ветка', self.run_task('run', '--continue-pr', success=False).stderr)
        self.assertEqual(self.state()['cycle_id'], self.initial['cycle_id'])

    def test_missing_pr_during_publish_does_not_create_replacement(self):
        self.change()
        self.run_task('run', '--continue-pr', '--local-only')
        (self.root / 'github-prs.json').write_text('[]')
        self.run_task('publish', success=False)
        self.assertNotIn('commit_sha', self.state())
        self.assertEqual(self.prs(), [])

    def test_reverting_previous_code_still_checks_cycle_scope(self):
        self.cfg['allow_paths'] = ['app/extra.txt']
        self.write_config()
        (self.worktree / 'app/name.txt').write_text('old name\n')
        self.assertIn('allow_paths', self.run_task('run', '--continue-pr', success=False).stderr)
        self.assertEqual(self.state()['phase'], 'published')

    def test_checks_resume_preserves_repair_budget(self):
        marker = self.root / 'broken-environment'
        marker.touch()
        self.cfg['checks'] = [{'name': 'environment', 'argv': [sys.executable, '-c',
                              f'from pathlib import Path; assert not Path({str(marker)!r}).exists()']}]
        self.cfg['max_check_fix_iterations'] = 1
        self.write_config()
        self.change()
        # Coding succeeds; the repair session fails before changing the candidate.
        self.env['FAIL_FIX'] = '1'
        self.run_task('run', '--continue-pr', success=False)
        state = self.state()
        self.assertEqual(state['check_fix_attempts'], 1)
        cycle = state['cycle_id']
        marker.unlink()
        self.run_task('run', '--continue-pr')
        self.assertEqual(self.state()['cycle_id'], cycle)
        self.assertEqual(self.state()['check_fix_attempts'], 1)

    def test_reverting_all_pr_changes_creates_delta_commit(self):
        self.env['NAME_CONTENT'] = 'old name\n'
        self.cfg['checks'][0]['argv'] = [sys.executable, '-c',
                                       "from pathlib import Path; assert Path('app/name.txt').read_text() == 'old name\\n'"]
        self.write_config()
        self.card.write_text(self.card.read_text() + '\nRevert the change.')
        self.run_task('run', '--continue-pr')
        state = self.state()
        self.assertNotEqual(state['commit_sha'], self.initial['commit_sha'])
        self.assertEqual(self.g(self.worktree, 'rev-parse', 'HEAD^{tree}'),
                         self.g(self.worktree, 'rev-parse', self.initial['base_sha'] + '^{tree}'))

    def test_invalid_flags_and_missing_state_start_no_models(self):
        self.assertIn('несовместим', self.run_task('run', '--continue-pr', '--rerun', success=False).stderr)
        self.assertIn('относятся к run', self.run_task('publish', '--continue-pr', success=False).stderr)
        self.card.write_text(self.card.read_text().replace(self.initial['issue'], 'AAS-999-new-task'))
        self.assertIn('сохранённое состояние', self.run_task('run', '--continue-pr', success=False).stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_planning_resume_runs_setup_once_and_honors_skip_review(self):
        self.change()
        self.write_card(issue=self.initial['issue'], complexity='medium')
        counter = self.root / 'setup-count'
        self.cfg['setup'] = [{'name': 'setup', 'argv': [sys.executable, '-c',
                              f'from pathlib import Path; p=Path({str(counter)!r}); p.write_text(p.read_text()+"x" if p.exists() else "x")']}]
        self.write_config()
        self.env['FAIL_STAGE'] = 'planning'
        self.run_task('run', '--continue-pr', '--skip-review', success=False)
        cycle = self.state()['cycle_id']
        self.env.pop('FAIL_STAGE')
        self.run_task('run', '--continue-pr', '--skip-review')
        self.assertEqual(counter.read_text(), 'x')
        self.assertEqual(self.state()['cycle_id'], cycle)
        self.assertEqual(self.state()['planning_status'], 'planned')
        self.assertEqual(self.state()['review_skip_reason'], '--skip-review')
        self.assertEqual([c['stage'] for c in self.calls()], ['coding', 'planning', 'planning', 'coding'])


if __name__ == '__main__':
    unittest.main()
