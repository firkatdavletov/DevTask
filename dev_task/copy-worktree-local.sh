#!/usr/bin/env bash
set -euo pipefail

MAIN_REPO_PATH="${1:?Usage: copy-worktree-local.sh MAIN_REPO_PATH WORKTREE_PATH}"
WORKTREE_PATH="${2:?Usage: copy-worktree-local.sh MAIN_REPO_PATH WORKTREE_PATH}"

# Copy local Codex configuration
if [[ -f "$MAIN_REPO_PATH/.codex/config.toml" ]]; then
    mkdir -p "$WORKTREE_PATH/.codex"
    cp "$MAIN_REPO_PATH/.codex/config.toml" \
       "$WORKTREE_PATH/.codex/config.toml"
fi

# Copy ignored personal skills only
SKILLS_DIR=".agents/skills"

for src in "$MAIN_REPO_PATH"/"$SKILLS_DIR"/*; do
    [[ -f "$src/SKILL.md" ]] || continue

    name="${src##*/}"
    relative="$SKILLS_DIR/$name"
    dest="$WORKTREE_PATH/$relative"

    # Skip tracked or non-ignored skills
    if ! git -C "$MAIN_REPO_PATH" check-ignore -q "$relative/SKILL.md"; then
        continue
    fi

    # Protect tracked or non-ignored skills in the target worktree
    if [[ -e "$dest" ]] && \
       ! git -C "$WORKTREE_PATH" check-ignore -q "$relative/SKILL.md"; then
        echo "Skip conflict: $name"
        continue
    fi

    mkdir -p "$dest"
    rsync -a "$src/" "$dest/"
    echo "Synced personal skill: $name"
done
