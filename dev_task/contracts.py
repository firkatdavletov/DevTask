"""Structured stage results, validation and shared result rules."""
from __future__ import annotations

import re
from pathlib import Path

from .errors import require


RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["implemented", "needs_input", "blocked"]},
        "summary": {"type": "string"},
        "manual_checks": {"type": "array", "items": {"type": "string"}},
        "verification_limits": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "manual_checks", "verification_limits", "blockers"],
    "additionalProperties": False,
}


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["reviewed", "needs_input", "blocked"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
            "file": {"type": "string"}, "line": {"type": "integer"},
            "explanation": {"type": "string"}},
            "required": ["priority", "file", "line", "explanation"], "additionalProperties": False}},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "findings", "blockers"],
    "additionalProperties": False,
}


PLANNING_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["planned", "needs_input", "blocked"]},
        "summary": {"type": "string"}, "plan": {"type": "string"},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "plan", "blockers"],
    "additionalProperties": False,
}


COMMIT_MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["generated"]},
                   "subject": {"type": "string"}},
    "required": ["status", "subject"],
    "additionalProperties": False,
}


RESULT_CONTRACT = """Контракт результата реализации и проверок:
blockers — только причины, мешающие завершить реализацию или исправление.
implemented требует пустой blockers; blocked и needs_input требуют непустой blockers.
verification_limits — список проверок, которые агент не смог выполнить, с причиной; иначе [].
Если реализация завершена, а проверка не запускается внутри песочницы (сокеты, сеть, права,
JDK/SDK), верни implemented, blockers: [] и опиши ограничение в verification_limits.
Незавершённая реализация и неустранённая ошибка обязательных проверок управляющего скрипта
остаются blockers. Не выдавай их за ограничение собственной песочницы.
Управляющий скрипт выполнит все config.checks в своём окружении; только их успех
разрешает дальнейшие этапы. Ограничения проверок агента допускаются при любом статусе.
"""


def validate_result(value):
    require(isinstance(value, dict) and set(value) == set(RESULT_SCHEMA["required"]), "Codex вернул некорректный результат.")
    require(value["status"] in ("implemented", "needs_input", "blocked") and isinstance(value["summary"], str), "Некорректный статус Codex.")
    for key in ("manual_checks", "verification_limits", "blockers"):
        require(isinstance(value[key], list) and all(isinstance(v, str) for v in value[key]), f"Некорректное поле {key}.")
    require((value["status"] == "implemented" and not value["blockers"])
            or (value["status"] != "implemented" and bool(value["blockers"])),
            "Для implemented blockers должен быть пустым; для blocked/needs_input нужны blockers.")
    return value


def validate_review(value):
    require(isinstance(value, dict) and set(value) == set(REVIEW_SCHEMA["required"]),
            "Codex вернул некорректный результат review.")
    require(value["status"] in ("reviewed", "needs_input", "blocked")
            and isinstance(value["summary"], str)
            and isinstance(value["blockers"], list)
            and all(isinstance(v, str) for v in value["blockers"])
            and isinstance(value["findings"], list), "Некорректный статус или поля review.")
    for finding in value["findings"]:
        require(isinstance(finding, dict) and set(finding) == {"priority", "file", "line", "explanation"}
                and finding["priority"] in ("P0", "P1", "P2", "P3")
                and isinstance(finding["file"], str) and finding["file"] and not Path(finding["file"]).is_absolute()
                and ".." not in Path(finding["file"]).parts
                and type(finding["line"]) is int and finding["line"] > 0
                and isinstance(finding["explanation"], str) and finding["explanation"].strip(),
                "Некорректное замечание review: нужны P0–P3, файл, строка и объяснение.")
    require((value["status"] == "reviewed" and not value["blockers"])
            or (value["status"] != "reviewed" and bool(value["blockers"])),
            "Некорректные блокеры review.")
    return value


def validate_planning(value):
    require(isinstance(value, dict) and set(value) == set(PLANNING_SCHEMA["required"]),
            "Codex вернул некорректный результат planning.")
    require(value["status"] in ("planned", "needs_input", "blocked")
            and isinstance(value["summary"], str) and isinstance(value["plan"], str)
            and isinstance(value["blockers"], list) and all(isinstance(v, str) for v in value["blockers"]),
            "Некорректные поля planning.")
    require((value["status"] == "planned" and value["plan"].strip() and not value["blockers"])
            or (value["status"] != "planned" and bool(value["blockers"])),
            "Для planned нужен непустой план без блокеров; для blocked/needs_input — blockers.")
    return value


def validate_commit_message(value):
    require(isinstance(value, dict) and set(value) == {"status", "subject"}
            and value["status"] == "generated", "Некорректный результат генерации сообщения коммита.")
    subject = value["subject"]
    require(isinstance(subject, str) and subject == subject.strip() and 1 <= len(subject) <= 160
            and len(subject.splitlines()) == 1
            and not any(ord(char) < 32 or ord(char) == 127 for char in subject)
            and re.search(r"[А-Яа-яЁё]", subject)
            and not re.match(r"(?:KRMA|AAS)-[0-9]+", subject),
            "Сообщение коммита: нужна одна строка на русском до 160 символов без номера Jira.")
    return value


def merge_implementation_results(previous, result, label):
    if not previous:
        return result
    return {**result, "summary": previous["summary"] + "\n\n" + label + result["summary"],
            **{key: list(dict.fromkeys(previous.get(key, []) + result.get(key, [])))
               for key in ("manual_checks", "verification_limits")}}


def blocking_findings(result):
    return [item for item in result["findings"] if item["priority"] in ("P0", "P1", "P2")]
