#!/usr/bin/env bash
# Capture this machine's current model picker settings into profile.json.
# Run this after changing model order, visibility, or display names.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required." >&2; exit 1; }

CODEX_PROFILE_REPO="$repo_dir" python3 - <<'PY'
import json, os

repo = os.environ["CODEX_PROFILE_REPO"]
opencodex_home = os.environ.get("OPENCODEX_HOME") or os.path.join(os.path.expanduser("~"), ".opencodex")
cfg = json.load(open(os.path.join(opencodex_home, "config.json"), encoding="utf-8"))

PROVIDERS = ["anthropic", "google-antigravity"]

# Only these paths travel between machines. Accounts, ports, credentials and the
# generated Codex catalog are deliberately excluded: they are machine-specific.
PATHS = [
    "modelPickerOrder",
    "disabledModels",
    "subagentModels",
    "showCodexSparkQuota",
    *[f"providers.{p}.{k}" for p in PROVIDERS
      for k in ("selectedModels", "modelDisplayNames", "defaultModel")],
    "providers.google-antigravity.alias",
    "providers.google-antigravity.googleMode",
]


def read(path):
    node = cfg
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


captured = {}
for path in PATHS:
    value = read(path)
    if value is None:
        print(f"skipped (absent): {path}")
        continue
    if path in ("modelPickerOrder", "disabledModels") and isinstance(value, list):
        value = [m for m in value if "/" not in m or m.split("/", 1)[0] in PROVIDERS]
    captured[path] = value

out = {"requiredProviders": PROVIDERS, "config": captured}
with open(os.path.join(repo, "profile.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(f"wrote profile.json with {len(captured)} settings")
PY
