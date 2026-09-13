#!/usr/bin/env bash
# Reproduce the saved Codex model picker + quota skill on this machine.
# Safe to re-run: every step is idempotent.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v ocx >/dev/null 2>command -v ocx >/dev/null 2>&1 || { echo "ocx not found. Install opencodex first." >&2; exit 1; }1 || {
  echo "OpenCodex (ocx) is required but was not found." >&2
  echo "Please install OpenCodex first, then re-run this script." >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || { echo "python3 is required." >&2; exit 1; }

CODEX_PROFILE_REPO="$repo_dir" python3 - <<'PY'
import hashlib, json, os, shutil, subprocess, sys, time

repo = os.environ["CODEX_PROFILE_REPO"]
codex_home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
profile = json.load(open(os.path.join(repo, "profile.json"), encoding="utf-8"))


def ocx(*args, check=True):
    return subprocess.run(["ocx", *args], capture_output=True, text=True, check=check)


def tree_digest(root):
    """Content hash of a directory, so an unchanged skill is not re-backed-up."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            h.update(os.path.relpath(path, root).encode())
            with open(path, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


# 1. Providers must exist before their models can be selected. OAuth login creates them.
missing = [p for p in profile["requiredProviders"]
           if ocx("config", "get", f"providers.{p}", check=False).returncode != 0]
if missing:
    print("Missing providers: " + ", ".join(missing), file=sys.stderr)
    print("Log in first, then re-run this script:", file=sys.stderr)
    for p in missing:
        print(f"  ocx login {p}", file=sys.stderr)
    sys.exit(1)

# 2. Apply the declarative settings through ocx so each value is schema-validated.
for path, value in profile["config"].items():
    ocx("config", "set", path, json.dumps(value, ensure_ascii=False))
    print(f"set {path}")

# 3. Install the skills, preserving anything already there under a timestamped backup.
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
    shutil.copytree(src, dst)
    print(f"installed skill {name}")

# 4. Rebuild the Codex catalog. The picker order lives here, not in any file we copy.
print(ocx("sync").stdout.strip())

# 5. Verify the order actually landed.
applied = json.loads(ocx("config", "get", "modelPickerOrder").stdout)
expected = profile["config"]["modelPickerOrder"]
print("modelPickerOrder matches profile:" , applied == expected)
if applied != expected:
    sys.exit(1)
PY

echo
echo "Done. Quit and reopen the Codex desktop app so its model picker reloads."
