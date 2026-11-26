# Test suite

The `unittest` suite covers the CLI, public API, and task workflow. `test_workflow.py` exercises the CLI against temporary Git repositories with fake Codex and GitHub commands. `test_architecture.py` checks import boundaries between package modules.

Run the suite from the repository root with `python3 -m unittest discover -s tests`.
