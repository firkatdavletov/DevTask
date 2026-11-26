# Python package

- `cli.py` handles command-line arguments and calls the public orchestration API in `api.py`.
- `workflow.py` coordinates the task stages implemented in `planning.py`, `coding.py`, `checks.py`, `review.py`, and `publishing.py`.
- `repository.py` manages Git worktrees, `codex.py` runs Codex, and `state.py` handles workflow state across runs.
