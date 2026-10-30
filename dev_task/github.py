"""GitHub CLI access and Draft PR identity checks."""
from __future__ import annotations

import json

from .errors import require
from .processes import capture


def gh(config, repo, *args):
    result = capture(["gh", *args, "--repo", config["github_repo"]], repo)
    return result.stdout.strip()


def find_pr(config, state):
    prs = json.loads(gh(config, config["repo"], "pr", "list", "--head", state["branch"], "--state", "all",
                        "--json", "number,url,baseRefName,headRefName,state,isDraft,isCrossRepository", "--limit", "100"))
    require(isinstance(prs, list), "Некорректный ответ gh pr list.")
    require(len(prs) <= 1, "Найдено несколько PR этой ветки; выберите нужный вручную.")
    if not prs:
        return None
    pr = prs[0]
    if state.get("pr"):
        require(pr["number"] == state["pr"]["number"] and pr["url"] == state["pr"]["url"],
                "Идентичность сохранённого PR изменилась.")
    require(pr.get("isCrossRepository") is False, "Найден PR из fork. Автоматическое продолжение отключено.")
    require(pr["baseRefName"] == state["base"] and pr["headRefName"] == state["branch"], "Существующий PR имеет другие ветки.")
    require(pr["state"] == "OPEN" and pr["isDraft"], "Существующий PR уже закрыт, слит или выведен из Draft.")
    return pr
