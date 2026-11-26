# Python-to-Go migration roadmap

This roadmap transitions the DevTask CLI to Go incrementally. The Python implementation is the behavioral reference throughout the migration. Follow the [migration rules](../PYTHON_TO_GO_MIGRATION_RULES.md) for every task.

The native Go CLI becomes the recommended macOS and Linux installation after the release gate passes. The existing Python API, `python -m dev_task`, and pip/pipx installations remain supported. Any retirement of those interfaces requires a separate decision.

## Working agreements

- Complete prerequisites before starting a dependent subtask. Several foundational subtasks can proceed independently after the differential harness exists.
- Keep the existing CLI arguments, exit codes, configuration formats, workflow semantics, state, reports, and worktrees compatible. Preserve retry limits, cancellation, and concurrency restrictions.
- Use idiomatic Go, prefer the standard library, use `context.Context` for cancellable operations, and place external processes behind testable interfaces.
- Add unit and compatibility tests for each migrated component. Compare the Go implementation with Python using equivalent fixtures. Automated tests use fake Codex and GitHub commands and never start real Codex sessions or publish real pull requests.
- For each migration task, run the existing Python suite, Go tests, relevant differential tests, and `go vet ./...`. Update this roadmap with the scope completed and any verified differences. Do not remove Python before behavioral parity is verified.
- Compare exit codes, stdout and stderr, progress and final reports, persisted artifacts, Git state, and external command order as applicable. Normalize generated IDs, temporary paths, and timing only where they vary between equivalent runs, while retaining their relationships.

## Subtasks

### 1. Establish the Python baseline

**Prerequisites** None.

**Reference** `tests/`, `README.md`, and the current Python implementation.

Run `python3 -m unittest discover -s tests` with Python 3.10 or newer, PyYAML from `pyproject.toml`, Git, Bash, and rsync. Include `test_workflow.WorkflowTest` in the run. Investigate any failures against the current Python behavior and record the reference revision and results.

**Acceptance** The full Python suite passes. The baseline revision, test command, and results are recorded below.

### 2. Bootstrap Go and differential testing

**Prerequisite** Subtask 1.

**Reference** `pyproject.toml`, `dev_task/cli.py`, and the fake commands and temporary repositories in `tests/test_workflow.py`.

Create a Go module and `cmd/dev-task` entry point. Build a harness that runs Python and Go against equivalent isolated fixtures and compares observable results. Reuse the existing fake Codex and GitHub behavior where practical.

**Acceptance** The harness runs both executables and detects a deliberately changed exit code or artifact. It does not contact real Codex or GitHub services.

**Status** Complete. Go 1.25 or newer builds the preview. It returns exit status 2 for a missing command and reports that other commands are unavailable. Subtask 3 ports the CLI arguments and diagnostics.

Build the preview into a separate directory and run the differential tests with the Python reference environment active.

```sh
go build -o build/dev-task-go ./cmd/dev-task
go test ./...
go vet ./...
DEV_TASK_GO_BINARY="$PWD/build/dev-task-go" python3 -m unittest discover -s tests -p test_differential.py -v
```

The comparison runs each command in a fresh fixture with a local bare Git remote and the Python suite's fake Codex and GitHub commands. It compares exit status, both output streams, checkout status, refs, run files, and recorded fake calls. The Go smoke test compares missing-command behavior and checks the preview's expected diagnostic. The offline report tests use Python on both sides to verify that equivalent runs match and that deliberate changes to exit status and a saved report are detected.

### 3. Port CLI inputs and configuration

**Prerequisite** Subtask 2.

**Reference** `dev_task/cli.py`, `api.py`, `card.py`, `config.py`, `models.py`, `tests/test_cli.py`, and `tests/test_api.py`.

Port argument parsing, help, task-card YAML, task-ID lookup, project and configuration paths, model profiles, validation, and child environment settings.

**Acceptance** Differential tests cover valid and invalid inputs, relative paths, profile precedence, deprecated configuration, and the Python CLI's exit codes. YAML behavior matches the existing PyYAML parser for supported cards.

### 4. Port process execution and progress

**Prerequisite** Subtask 2.

**Reference** `dev_task/processes.py`, `events.py`, `console.py`, and `tests/test_console.py`.

Implement a testable external-process interface using `context.Context`. Preserve arguments, working directories, child environments, output logs, timeouts, progress events, and terminal rendering.

**Acceptance** Differential tests cover normal and redirected output, failures, and interruption. A cancellation test confirms that a spawned descendant is cleaned up with its process group.

### 5. Port storage and workflow state

**Prerequisite** Subtask 2.

**Reference** `dev_task/storage.py`, `state.py`, `workflow.py`, and state compatibility cases in `tests/`.

Port path resolution, repository keys, atomic JSON writes, digests, state upgrades through workflow version 4, and cycle history. Preserve historical fields and their defaults.

**Acceptance** Compatibility tests cover older states, missing fields, rejection of future versions, and Python reading state written by Go. Saved run and worktree paths remain usable.

### 6. Deliver offline report

**Prerequisites** Subtasks 3, 4, and 5.

**Reference** `dev_task/reports.py`, `usage.py`, `sessions.py`, `tests/test_usage_reports.py`, and `tests/test_cli.py`.

Rebuild `report.json` and `report.md` from existing state and logs, including branch reviews and session usage.

**Acceptance** Differential tests cover historical and partial runs, cached tokens, unavailable usage, and final report output. Reporting succeeds with a removed checkout, unavailable external tools, and all workflow slots occupied.

### 7. Port Git worktrees, locks, and snapshots

**Prerequisites** Subtasks 4 and 5.

**Reference** `dev_task/repository.py`, `copy-worktree-local.sh`, `tests/test_copy_worktree_local.py`, and `tests/test_workflow.py`.

Port remote validation, worktree creation and reuse, file and Git snapshots, path restrictions, and repository locking. Package the existing copy helper and retain its retry behavior.

**Acceptance** Tests cover existing and new worktrees, copy failures and retries, and changes to files, index, branch, or HEAD. Concurrent Python and Go processes share the per-issue lock, three repository slots, and Git write lock across linked worktrees.

### 8. Port Codex sessions

**Prerequisites** Subtasks 3, 4, 6, and 7.

**Reference** `dev_task/codex.py`, `sessions.py`, `contracts.py`, `usage.py`, and the planning, review, and usage tests.

Port stage-specific arguments, prompts, sandboxes, result schemas and validation, session logs, and usage snapshots.

**Acceptance** Fake-session differential tests cover successful and malformed results, unavailable usage, timeout, interruption, and the resulting persisted artifacts. Planning and coding use distinct sessions.

### 9. Deliver standalone branch review

**Prerequisite** Subtask 8.

**Reference** `dev_task/branch_review.py` and `tests/test_branch_review.py`.

Port remote-ref fetching, merge-base selection, detached worktree handling, review execution, findings, and reports.

**Acceptance** Differential tests cover empty diffs, blocking and nonblocking findings, interruption, and clean and modified worktree cleanup. Exit codes and saved review artifacts match Python.

### 10. Port setup, planning, and coding

**Prerequisite** Subtask 8.

**Reference** `dev_task/workflow.py`, `planning.py`, `plan_context.py`, `coding.py`, `tests/test_planning.py`, and `tests/test_verification_limits.py`.

Implement the initial stages of `run`, including setup reuse, planning skips, plan integrity, coding results, blockers, and verification limits.

**Acceptance** Tests resume after failure at each stage, preserve completed setup and planning, reject a modified plan, and keep blockers separate from verification limits.

### 11. Port checks and check repairs

**Prerequisite** Subtask 10.

**Reference** `dev_task/checks.py`, `tests/test_check_repairs.py`, and `tests/test_check_resume.py`.

Port checks, staging and snapshot validation, logs, and the independent check-repair budget.

**Acceptance** Tests interrupt and resume a repair, preserve its attempt count and completed coding result, and rerun all required checks. Source or Git-state changes made by checks stop the workflow as in Python.

### 12. Complete local runs

**Prerequisite** Subtask 11.

**Reference** `dev_task/review.py`, `workflow.py`, `tests/test_code_review.py`, and `tests/test_workflow.py`.

Port task review, review repairs, and the remaining `run --local-only` orchestration.

**Acceptance** P0–P2 findings trigger the appropriate fixes and fresh checks, P3 findings are recorded, and review remains tied to the checked snapshot. Tests cover skipped review, independent repair budgets, and `--rerun` history.

### 13. Deliver publication

**Prerequisite** Subtask 12.

**Reference** `dev_task/publishing.py`, `github.py`, `tests/test_commit_messages.py`, and publication cases in `tests/test_workflow.py`.

Port checked-tree validation, commit-message generation and reuse, Git hooks, push, and Draft PR lookup and creation. Support both `publish` and automatic publication after `run`.

**Acceptance** Tests use a local bare remote and fake GitHub commands. Failed push, commit hook failure, and a lost PR response can be retried without duplicate commits or PRs. Changes to the checked snapshot prevent publication.

### 14. Deliver PR continuation

**Prerequisite** Subtask 13.

**Reference** `dev_task/workflow.py`, `publishing.py`, `state.py`, and `tests/test_continue_pr.py`.

Port `--continue-pr`, including the existing Draft PR, worktree edits, cycle history, and the different diff scopes for review and the current commit.

**Acceptance** Tests cover a no-change cycle, remote divergence, interrupted continuation, and local-only continuation followed by `publish`. Both implementations can resume the same persisted continuation.

### 15. Verify interoperability and release

**Prerequisites** Subtasks 9 and 14.

**Reference** The preceding compatibility tests, `README.md`, and `WORKFLOW.md`.

Run the complete parity matrix on macOS and Linux. Build and smoke-test native binaries, document installation and rollback, and make the native CLI the recommended installation after the gates pass.

**Acceptance** Python-to-Go and Go-to-Python handoffs succeed after interrupted coding, repair, and publication, using the same saved state and worktree. Full Python, Go, differential, and `go vet` checks pass on supported platforms. Existing Python installations and the importable API remain supported.

## Baseline status

The reference source and test revision is `62c9577cd24a2aa6705fed3bc36c250112481b1b`. The reference run uses Python 3.12.14 and PyYAML 6.0.3 on macOS 15.6.1 (arm64).

```sh
GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 python3 -m unittest discover -s tests -v
```

**Result** All 219 tests passed. An earlier run hit a transient permission error in the fake Codex app-server during PR continuation. The affected test passed on its own and in the full rerun.

Commit `6fc1081b34b3a8f156e3fe726ec702eebead9e77` reconciled the historical `WorkflowTest` expectations with the current Python workflow, including the persisted state and report formats. The reference run includes that class.
