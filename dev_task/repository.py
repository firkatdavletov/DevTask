"""Git worktrees, repository locks and snapshot invariants."""
from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from .errors import TaskError, require
from .processes import capture
from .storage import file_digest, read_json, resolve_path, write_json


def git(cwd, *args):
    return capture(["git", *args], cwd).stdout.strip()


@contextlib.contextmanager
def repository_lock(repo):
    common = resolve_path(git(repo, "rev-parse", "--git-common-dir"), repo)
    with (common / "dev-task.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TaskError("Для этого репозитория уже работает dev-task.")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def remote_identity(url):
    # Accept SSH scp syntax and HTTPS / ssh://. Compare identity without printing URL.
    if "://" in url:
        parsed = urlsplit(url)
        host, path = parsed.hostname, parsed.path.strip("/")
    else:
        match = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", url)
        require(match is not None, "remote должен указывать на GitHub через SSH или HTTPS.")
        host, path = match.group(1), match.group(2).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    require(host and len(path.split("/")) == 2, "Не удалось определить GitHub-репозиторий remote.")
    return f"{host}/{path}".lower()


def assert_worktree(state):
    worktree = Path(state["worktree"])
    require(worktree.is_dir(), "Сохранённый worktree не найден; восстановите его вручную.")
    require(git(worktree, "rev-parse", "--show-toplevel") == str(worktree), "Некорректный worktree.")
    require(git(worktree, "branch", "--show-current") == state["branch"], "В worktree изменилась рабочая ветка.")
    expected = state.get("commit_sha") or state.get("cycle_base_sha", state["base_sha"])
    require(git(worktree, "rev-parse", "HEAD") == expected, "HEAD изменился вне dev-task. Проверьте историю вручную.")
    return worktree


def assert_remote(config):
    repo, remote = config["repo"], config["remote"]
    urls = git(repo, "remote", "get-url", "--all", remote).splitlines()
    push_urls = git(repo, "remote", "get-url", "--push", "--all", remote).splitlines()
    require(urls and push_urls and all(remote_identity(url) == config["github_repo"].lower() for url in urls + push_urls),
            "github_repo не совпадает с одним из fetch/push URL Git remote.")


def prepare(config, card, run_dir, state_path):
    repo, remote = config["repo"], config["remote"]
    require(git(repo, "rev-parse", "--show-toplevel") == str(repo), "config.repo должен быть корнем checkout.")
    assert_remote(config)
    git(repo, "fetch", "--prune", remote)
    ref = f"refs/remotes/{remote}/{card['base']}"
    base_sha = git(repo, "rev-parse", "--verify", ref + "^{commit}")
    if state_path.exists():
        state = read_json(state_path)
        for key, expected in (("repo", str(repo)), ("base", card["base"]), ("issue", card["issue"]),
                              ("remote", remote), ("github_repo", config["github_repo"])):
            require(state.get(key) == expected, f"Сохранённая задача имеет другое поле {key}.")
        assert_worktree(state)
        return state
    branch = "feature/" + card["issue"]
    worktree = config["worktrees_root"] / card["issue"]
    require(not worktree.exists(), "Каталог worktree уже существует без state.json. Проверьте его вручную.")
    require(capture(["git", "show-ref", "--verify", "--quiet", "refs/heads/" + branch], repo, ok=False).returncode != 0,
            "Локальная ветка уже существует без состояния dev-task.")
    require(not git(repo, "ls-remote", "--heads", remote, "refs/heads/" + branch), "Ветка уже существует на remote. Автоматический перехват отключён.")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(worktree), base_sha)
    state = {"issue": card["issue"], "repo": str(repo), "remote": remote, "github_repo": config["github_repo"],
             "base": card["base"], "base_sha": base_sha, "branch": branch, "worktree": str(worktree), "phase": "prepared"}
    write_json(state_path, state)
    return state


def readonly_snapshot(worktree):
    """Preserve dirty tracked files, untracked files and the index without staging."""
    paths = capture(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], worktree).stdout
    files = {}
    for name in sorted(set(paths.split("\0")) - {""}):
        path = worktree / name
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            files[name] = {"kind": "missing"}
            continue
        record = {"mode": stat.S_IMODE(mode)}
        if stat.S_ISLNK(mode):
            record.update(kind="symlink", target=os.readlink(path))
        elif stat.S_ISREG(mode):
            record.update(kind="file", sha256=file_digest(path))
        else:
            record["kind"] = "directory" if stat.S_ISDIR(mode) else "special"
        files[name] = record
    index = resolve_path(git(worktree, "rev-parse", "--git-path", "index"), worktree)
    return {"head": git(worktree, "rev-parse", "HEAD"), "branch": git(worktree, "branch", "--show-current"),
            "index": file_digest(index) if index.exists() else None, "files": files}


def changed_paths(worktree, base_sha):
    # --no-renames checks both deleted and added sides, preventing rename scope escapes.
    tracked = capture(["git", "diff", "--no-renames", "--name-only", "-z", base_sha], worktree).stdout
    untracked = capture(["git", "ls-files", "--others", "--exclude-standard", "-z"], worktree).stdout
    return sorted(set(v for v in (tracked + untracked).split("\0") if v))


def task_changed_paths(worktree, state):
    return sorted(set(changed_paths(worktree, state["base_sha"])) |
                  set(changed_paths(worktree, state.get("cycle_base_sha", state["base_sha"]))))


def assert_scope(config, paths):
    for path in paths:
        require(any(fnmatch.fnmatchcase(path, pat) for pat in config["allow_paths"]), f"Путь вне allow_paths: {path}")
        require(not any(fnmatch.fnmatchcase(path, pat) for pat in config["deny_paths"]), f"Путь запрещён deny_paths: {path}")


def assert_unchanged_checks(state, worktree, expected_tree):
    assert_worktree(state)
    require(git(worktree, "write-tree") == expected_tree and
            capture(["git", "diff", "--quiet"], worktree, ok=False).returncode == 0 and
            not git(worktree, "ls-files", "--others", "--exclude-standard"),
            "Проверки изменили исходники или index. Проверьте diff и повторите запуск.")


def assert_published_remote(config, state):
    output = git(config["repo"], "ls-remote", "--heads", config["remote"], "refs/heads/" + state["branch"])
    remote_sha = output.split()[0] if output else None
    allowed = {state["last_published_sha"]}
    if state.get("commit_sha"):
        allowed.add(state["commit_sha"])
    require(remote_sha is not None and remote_sha in allowed,
            "Удалённая ветка изменилась или отсутствует. Проверьте историю вручную.")
