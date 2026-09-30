#!/usr/bin/env bash
# Bootstrap this machine: picker policy on top of OpenCodex's live catalog + quota skill.
# Safe to re-run: every step is idempotent and user-disabled models are never re-enabled.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v ocx >/dev/null 2>&1 || {
  echo "OpenCodex (ocx) is required but was not found." >&2
  echo "Please install OpenCodex first, then re-run this script." >&2
  exit 1
}
py="$(command -v python3 || command -v python || true)"
[ -n "$py" ] || { echo "python3 is required." >&2; exit 1; }

# Provider check, policy settings, legacy selectedModels migration, first reconcile, ocx sync.
# Stops before writing anything when a provider login is missing or discovery fails.
"$py" "$repo_dir/scripts/reconcile-models.py" --bootstrap

# Install the skills, preserving anything already there under a timestamped backup.
CODEX_PROFILE_REPO="$repo_dir" "$py" - <<'PY'
import hashlib, os, shutil, time

repo = os.environ["CODEX_PROFILE_REPO"]
codex_home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def tree_digest(root):
    """Content hash of a directory, so an unchanged skill is not re-backed-up."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            h.update(os.path.relpath(path, root).encode())
            with open(path, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


skills_src = os.path.join(repo, "skills")
skills_dst = os.path.join(codex_home, "skills")
os.makedirs(skills_dst, exist_ok=True)
for name in sorted(os.listdir(skills_src)):
    src, dst = os.path.join(skills_src, name), os.path.join(skills_dst, name)
    if not os.path.isdir(src):
        continue
    if os.path.exists(dst):
        if tree_digest(src) == tree_digest(dst):
            print(f"skill {name} already current")
            continue
        backup = f"{dst}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.move(dst, backup)
        print(f"existing skill moved to {backup}")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    print(f"installed skill {name}")
PY

# Verify: a second reconcile pass must find nothing to change.
"$py" "$repo_dir/scripts/reconcile-models.py" --check

echo
echo "Done. Quit and reopen the Codex desktop app so its model picker reloads."
