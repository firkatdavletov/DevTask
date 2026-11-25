"""Codex CLI invocation with structured results."""
from __future__ import annotations


from .events import step, emit
from .errors import require
from .processes import execute
from .repository import assert_worktree
from .sessions import tracked_session
from .storage import read_json, write_json
from .usage import STAGE_NAMES, event_usage


def launch_codex(config, state, directory, stage, prompt, schema, validator, sandbox):
    with step('Подготовка сессии агента'):
        worktree = assert_worktree(state)
    directory.mkdir(parents=True, exist_ok=True)
    schema_path, result_path = directory / "schema.json", directory / "result.json"
    write_json(schema_path, schema)
    (directory / "prompt.md").write_text(prompt, encoding="utf-8")
    role = {"planning": "planning", "review": "review", "commit_message": "text"}.get(stage, "coding")
    profile = state["execution_profile"][role]
    args = config["stage_codex_args"]["review" if sandbox == "read-only" else "coding"]
    argv = [config["codex_bin"], "exec", "--model", profile["model"],
            "-c", "model_reasoning_effort=" + profile["effort"], "--sandbox", sandbox, "--json",
            "--output-schema", str(schema_path), "-o", str(result_path), *args, "-"]
    emit(f"Codex {STAGE_NAMES[stage]}. Журнал: {directory / 'events.jsonl'}",
         kind="agent", stage=stage, status="started", directory=str(directory),
         model=profile["model"], effort=profile["effort"])
    with tracked_session(config, state, directory, worktree, stage) as record:
        record.update(model=profile["model"], effort=profile["effort"], cycle_id=state["cycle_id"])
        with step(f"{STAGE_NAMES[stage]} · модель: {profile['model']} · глубина: {profile['effort']}"):
            code = execute(argv, worktree, directory / "events.jsonl", config["codex_timeout_seconds"],
                           input_text=prompt, stderr_log=directory / "codex.stderr.log")
            require(code == 0, f"Codex завершился с кодом {code}. См. {directory / 'codex.stderr.log'}")
        record["exit_code"] = code
        _, session_id = event_usage(directory / "events.jsonl", "running")
        if session_id:
            state["session_id"] = session_id
        with step('Обработка результата агента'):
            result = validator(read_json(result_path))
        record["status"] = result["status"]
        return result
