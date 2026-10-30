"""Configuration validation and execution-profile selection."""
from __future__ import annotations

import re

from .errors import TaskError, require
from .storage import read_json, resolve_path


DEFAULT_MODELS = {"luna": "gpt-6-luna", "sol": "gpt-6.1-sol", "astra": "gpt-6-astra"}


DEFAULT_PROFILES = {
    "trivial": {"planning": None, "coding": {"model": "luna", "effort": "low"}, "review": None},
    "simple": {"planning": None, "coding": {"model": "luna", "effort": "medium"}, "review": {"model": "sol", "effort": "low"}},
    "medium": {"planning": {"model": "sol", "effort": "medium"}, "coding": {"model": "sol", "effort": "medium"}, "review": {"model": "astra", "effort": "low"}},
    "complex": {"planning": {"model": "astra", "effort": "high"}, "coding": {"model": "sol", "effort": "high"}, "review": {"model": "astra", "effort": "medium"}},
}


def load_config(path):
    config = read_json(path)
    require(isinstance(config, dict), "Конфигурация должна быть JSON-объектом.")
    for key in ("repo", "github_repo", "worktrees_root", "runs_root"):
        require(isinstance(config.get(key), str) and config[key], f"Нужен параметр config.{key}.")
    for key in ("repo", "worktrees_root", "runs_root"):
        config[key] = resolve_path(config[key], path.parent)
    require(re.fullmatch(r"[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config["github_repo"]),
            "github_repo: HOST/OWNER/REPOSITORY, например github.company/mobile/android.")
    if "android_home" in config:
        require(isinstance(config["android_home"], str) and config["android_home"].strip(),
                "android_home: непустая строка с путём к Android SDK.")
        config["android_home"] = resolve_path(config["android_home"], path.parent)
    config.setdefault("remote", "origin")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", config["remote"]), "Некорректное имя remote.")
    config.setdefault("codex_bin", "codex")
    config.setdefault("codex_args", [])
    require(isinstance(config["codex_args"], list) and all(isinstance(v, str) for v in config["codex_args"]), "codex_args: массив строк.")
    for key in ("allow_paths", "deny_paths"):
        config.setdefault(key, ["**"] if key == "allow_paths" else [])
        require(isinstance(config[key], list) and all(isinstance(v, str) for v in config[key]), f"{key}: массив строк.")
    config.setdefault("setup", [])
    require(isinstance(config.get("checks"), list) and config["checks"], "Нужна хотя бы одна обязательная проверка checks.")
    for group in ("setup", "checks"):
        require(isinstance(config[group], list), f"{group}: массив команд.")
        for item in config[group]:
            require(isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"], f"{group}: нужно name.")
            require(isinstance(item.get("argv"), list) and item["argv"] and all(isinstance(v, str) and v for v in item["argv"]), f"{group}: argv — непустой массив строк.")
            item.setdefault("timeout_seconds", 1800)
            require(type(item["timeout_seconds"]) is int and item["timeout_seconds"] > 0, "timeout_seconds: положительное целое.")
    config.setdefault("codex_timeout_seconds", 3600)
    require(type(config["codex_timeout_seconds"]) is int and config["codex_timeout_seconds"] > 0, "Неверный codex_timeout_seconds.")
    config.setdefault("max_check_fix_iterations", 3)
    require(type(config["max_check_fix_iterations"]) is int and config["max_check_fix_iterations"] >= 0,
            "max_check_fix_iterations: целое число >= 0.")
    config.setdefault("max_fix_iterations", 3)
    require(type(config["max_fix_iterations"]) is int and config["max_fix_iterations"] >= 0,
            "max_fix_iterations: целое число >= 0.")
    config["stage_codex_args"] = validate_codex_args(config["codex_args"])
    return config


def validate_codex_args(values):
    coding, review = [], []
    index = 0
    while index < len(values):
        arg = values[index]
        start = index
        if arg in ("--add-dir", "-c", "--config", "--enable", "--disable", "--color"):
            require(index + 1 < len(values), f"После {arg} нужно значение в codex_args.")
            index += 1
            value = values[index]
        elif arg.startswith(("--add-dir=", "--config=", "--enable=", "--disable=", "--color=")):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-c") and arg != "-c":
            value = arg[2:]
        elif arg in ("--ephemeral", "--strict-config", "--ignore-user-config", "--ignore-rules"):
            value = None
        else:
            raise TaskError(f"Конфликтующий или неподдерживаемый codex_args: {arg}. Модель, глубину и sandbox задаёт dev-task.")
        require(value is None or bool(value), f"Пустое значение codex_args: {arg}.")
        if arg in ("-c", "--config") or arg.startswith(("-c", "--config=")):
            key = value.split("=", 1)[0].strip().lower()
            require("=" in value and key not in ("model", "model_reasoning_effort", "sandbox", "approval_policy", "cwd")
                    and not key.startswith(("sandbox_", "model_", "approval_")),
                    f"codex_args переопределяет модель, глубину или sandbox: {arg} {value}.")
        chunk = values[start:index + 1]
        coding.extend(chunk)
        if not (arg == "--add-dir" or arg.startswith("--add-dir=")):
            review.extend(chunk)
        index += 1
    return {"coding": coding, "review": review}


def resolve_profile(config, card):
    complexity = card.get("complexity")
    require(isinstance(complexity, str) and complexity.strip().lower() in DEFAULT_PROFILES,
            "Карточке нужно поле complexity: trivial, simple, medium или complex.")
    complexity = complexity.strip().lower()
    aliases = config.get("models", {})
    require(isinstance(aliases, dict) and all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                             for k, v in aliases.items()), "models: объект с именами моделей.")
    aliases = {**DEFAULT_MODELS, **aliases}
    overrides = config.get("complexity_profiles", {})
    require(isinstance(overrides, dict) and all(k in DEFAULT_PROFILES and isinstance(v, dict)
                                                 for k, v in overrides.items()),
            "complexity_profiles: объект с известными сложностями.")
    chosen = overrides.get(complexity, {})
    require(all(k in ("coding", "review", "planning") for k in chosen),
            "complexity_profiles: неизвестный этап.")
    result = {"complexity": complexity}
    for stage in ("planning", "coding", "review"):
        value = chosen.get(stage, DEFAULT_PROFILES[complexity][stage])
        require(stage != "coding" or value is not None, "Этап coding пропускать нельзя.")
        if value is None:
            result[stage] = None
            continue
        require(isinstance(value, dict) and set(value) == {"model", "effort"}
                and isinstance(value["model"], str) and value["model"].strip()
                and value["effort"] in ("low", "medium", "high", "xhigh", "max", "ultra"),
                f"{stage}: нужны model и корректный effort.")
        result[stage] = {"model": aliases.get(value["model"], value["model"]), "effort": value["effort"]}
    return result
