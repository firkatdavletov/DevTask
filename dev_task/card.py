"""Task-card parsing and validation."""
from __future__ import annotations

import re
import yaml

from .errors import require


def read_card(path):
    text = path.read_text(encoding="utf-8")
    parts = text.splitlines(keepends=True)
    require(parts and parts[0].strip() == "---", "Карточка должна начинаться с YAML-блока ---.")
    end = next((i for i in range(1, len(parts)) if parts[i].strip() == "---"), None)
    require(end is not None, "Не найден конец YAML-блока.")
    meta = yaml.safe_load("".join(parts[1:end]))
    require(isinstance(meta, dict), "Метаданные должны быть YAML-объектом.")
    for key in ("issue", "base", "title"):
        require(isinstance(meta.get(key), str) and meta[key].strip(), f"Нужно строковое поле {key}.")
    require(re.fullmatch(r"(?:KRMA|AAS)-[1-9][0-9]*(?:-[a-z0-9]+)*", meta["issue"]),
            "Ожидается KRMA-1234 или AAS-5678 с описанием через дефисы, например KRMA-10003-add-username-validation-error.")
    require(meta["base"] == "develop" or re.fullmatch(r"feature/(?:KRMA|AAS)-[1-9][0-9]*", meta["base"]),
            "base: develop или feature/KRMA-1234 (либо AAS).")
    require(meta["base"] != "feature/" + meta["issue"], "Рабочая и целевая ветки должны отличаться.")
    require("\n" not in meta["title"] and "\r" not in meta["title"], "Заголовок должен быть однострочным.")
    require("".join(parts[end + 1:]).strip(), "Описание задачи пустое.")
    require(not meta.get("jira") or isinstance(meta["jira"], str), "jira должна быть строкой.")
    return meta, text
