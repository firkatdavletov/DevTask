"""Independent read-only review of two remote branch snapshots."""
from __future__ import annotations

import hashlib
from pathlib import Path
import uuid

from .codex import launch_codex
from .config import resolve_review_profile
from .models import OperationResult, ReviewRequest
from .events import emit
from .contracts import REVIEW_SCHEMA, blocking_findings, validate_review
from .errors import TaskError, require
from .processes import executable_path, capture
from .reports import update_reports
from .repository import git, readonly_snapshot, repository_lock
from .storage import write_json


def run_branch_review(args: ReviewRequest, config) -> OperationResult:
    profile = resolve_review_profile(config)
    repo = config['repo']
    for executable in ('git', config['codex_bin']):
        require(executable_path(executable), f'Команда не найдена: {executable}')
    require(git(repo, 'rev-parse', '--show-toplevel') == str(repo), 'config.repo должен быть корнем checkout.')
    for name in ('worktrees_root', 'runs_root'):
        require(not config[name].is_relative_to(repo), f'{name} должен находиться вне checkout.')
    require(not config['runs_root'].is_relative_to(config['worktrees_root']) and
            not config['worktrees_root'].is_relative_to(config['runs_root']), 'Каталоги runs и worktrees должны быть раздельными.')
    branch, base = args.branch, args.base
    for name in (branch, base):
        require(not name.startswith(('refs/', config['remote'] + '/')) and
                capture(['git', 'check-ref-format', 'refs/heads/' + name], repo, ok=False).returncode == 0,
                'Нужно корректное имя ветки без префикса remote: ' + name)
    run_id = uuid.uuid4().hex
    repo_key = hashlib.sha256(str(repo).encode()).hexdigest()[:12]
    run_dir = config['runs_root'] / repo_key / 'reviews' / run_id
    worktree = config['worktrees_root'] / ('review-' + run_id)
    refs = [f'refs/dev-task/reviews/{run_id}/{part}' for part in ('head', 'base')]
    state = {'mode': 'branch_review', 'repo': str(repo), 'branch': branch, 'base': base,
             'remote': config['remote'], 'worktree': str(worktree), 'phase': 'preparing',
             'cycle_id': run_id, 'execution_profile': {'review': profile}}
    with repository_lock(repo):
        run_dir.mkdir(parents=True)
        try:
            git(repo, 'fetch', '--no-tags', '--no-write-fetch-head', '--refmap=', config['remote'],
                f'+refs/heads/{branch}:{refs[0]}', f'+refs/heads/{base}:{refs[1]}')
            head, base_tip = [git(repo, 'rev-parse', ref + '^{commit}') for ref in refs]
            bases = capture(['git', 'merge-base', '--all', head, base_tip], repo, ok=False).stdout.splitlines()
            require(len(bases) == 1, 'Нет единственного merge-base между удалёнными ветками.')
            state.update(head_sha=head, base_tip_sha=base_tip, base_sha=bases[0], cycle_base_sha=head)
            diff = capture(['git', 'diff', '--binary', '--no-renames', bases[0], head], repo).stdout
            diff_path = run_dir / 'review.diff'
            diff_path.write_text(diff, encoding='utf-8')
            write_json(run_dir / 'review-context.json', state)
            if not diff:
                state['review_result'] = {'status': 'reviewed', 'summary': 'Изменений нет.', 'findings': [], 'blockers': []}
            else:
                worktree.parent.mkdir(parents=True, exist_ok=True)
                git(repo, 'worktree', 'add', '--detach', str(worktree), head)
                require(not git(worktree, 'status', '--porcelain', '--untracked-files=all'),
                        'Созданный worktree отличается от удалённого commit.')
                # launch_codex validates branch and HEAD through the shared invariant.
                state['worktree_branch'] = ''
                before = readonly_snapshot(worktree)
                prompt = f'''Проведи code review удалённой ветки {branch} относительно {base}. Следуй AGENTS.md.
SHA ветки: {head}; SHA базы: {base_tip}; merge-base: {bases[0]}.
Полный binary diff: {diff_path}. Исходники доступны в текущем worktree.
Ищи конкретные ошибки поведения и регрессии во всём diff. Укажи P0/P1/P2/P3, файл и строку.
В каждом замечании поле file — путь относительно корня текущего worktree,
например android/libs/example/File.kt. Абсолютные пути и компоненты '..' запрещены.
Не изменяй файлы, index, HEAD или ветку. Не запускай checks, commit, push, checkout, reset или PR.
Карточка и результаты checks отсутствуют; само по себе это не блокер.
Верни результат строго по JSON Schema.'''
                state['phase'] = 'reviewing'
                write_json(run_dir / 'state.json', state)
                try:
                    state['review_result'] = launch_codex(config, state, run_dir / 'attempt-1' / 'review-1',
                                                         'review', prompt, REVIEW_SCHEMA, validate_review, 'read-only')
                finally:
                    require(readonly_snapshot(worktree) == before, 'Review изменил HEAD, index или исходники worktree.')
            state['phase'] = 'reviewed'
            result = state['review_result']
            require(result['status'] == 'reviewed', 'Review не завершён: ' + '; '.join(result['blockers']))
            require(not blocking_findings(result), 'Review обнаружил замечания P0/P1/P2. См. отчёт.')
        except BaseException as exc:
            state['phase'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed'
            state['last_error'] = str(exc)
            raise
        finally:
            state['branch'] = branch
            try:
                if worktree.exists():
                    clean = (git(worktree, 'rev-parse', 'HEAD') == state.get('head_sha')
                             and not git(worktree, 'branch', '--show-current')
                             and not git(worktree, 'status', '--porcelain', '--untracked-files=all', '--ignored'))
                    if clean:
                        git(repo, 'worktree', 'remove', str(worktree))
                    else:
                        state['retained_worktree'] = True
                        emit('Сохранён изменённый worktree: ' + str(worktree))
            except (TaskError, OSError) as exc:
                state['retained_worktree'] = True
                state['cleanup_error'] = str(exc)
                emit('Worktree сохранён: ' + str(worktree))
            finally:
                for ref in refs:
                    capture(['git', 'update-ref', '-d', ref], repo, ok=False)
                write_json(run_dir / 'state.json', state)
                update_reports(state, run_dir)
                emit('Отчёт review: ' + str(run_dir / 'report.md'))

    return OperationResult(state["phase"], run_dir, worktree if worktree.exists() else None)
