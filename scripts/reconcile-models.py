#!/usr/bin/env python3
"""Refine the OpenCodex model catalog for the Codex picker.

OpenCodex owns the model inventory (live discovery) and visibility (disabledModels).
This script only derives readable display names and a stable picker order from the
live catalog and policy.json. It never enables a model and never rewrites
disabledModels outside the one-time --bootstrap migration, which only adds entries.

  reconcile-models.py              name + order new models, ocx sync when changed
  reconcile-models.py --bootstrap  also: provider check, policy settings, legacy migration
  reconcile-models.py --check      exit 1 if names/order are not what reconcile would write
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

POLICY_PATH = Path(__file__).resolve().parent.parent / "policy.json"

DATE = re.compile(r"^\d{8}$")
QUALIFIERS = {"thinking", "minimal", "low", "medium", "high", "xhigh", "tiered"}
VALID_ID = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
VERSION = re.compile(r"^\d+(?:\.\d+)*$")
SIZE = re.compile(r"^\d+(?:\.\d+)?[bkm]$")
ACRONYMS = {"gpt", "oss"}


# ---------------------------------------------------------------- pure logic

def read_path(data: Mapping[str, Any], path: str) -> Any:
    node: Any = data
    for key in path.split("."):
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def split_qualifiers(model_id: str) -> Tuple[str, List[str]]:
    """'claude-opus-4-6-thinking' -> ('claude-opus-4-6', ['thinking'])."""
    tokens = model_id.split("-")
    tail: List[str] = []
    while len(tokens) > 1 and (tokens[-1] in QUALIFIERS or DATE.match(tokens[-1])):
        tail.insert(0, tokens.pop())
    return "-".join(tokens), tail


def _strip_prefix(base: str, rules: Mapping[str, Any]) -> str:
    prefix = rules.get("stripPrefix", "")
    return base[len(prefix):] if prefix and base.startswith(prefix) and len(base) > len(prefix) else base


def humanize(model_id: str, rules: Mapping[str, Any]) -> Optional[str]:
    """Readable name from the id alone, or None when the id is not safely parseable."""
    base, _ = split_qualifiers(model_id)
    if not VALID_ID.match(base):
        return None
    parts: List[List[str]] = []  # [kind, text]
    for tok in _strip_prefix(base, rules).split("-"):
        last = parts[-1] if parts else None
        if VERSION.match(tok):
            if last and last[0] == "v":
                last[1] += "." + tok
            else:
                parts.append(["v", tok])
        elif tok in ACRONYMS:
            if last and last[0] == "a":
                last[1] += "-" + tok.upper()
            else:
                parts.append(["a", tok.upper()])
        elif SIZE.match(tok):
            parts.append(["w", tok.upper()])
        else:
            parts.append(["w", tok.capitalize()])
    name = " ".join(text for _, text in parts)
    prefix = rules.get("namePrefix", "")
    if prefix and base.split("-")[0] not in rules.get("namePrefixSkip", []):
        name = prefix + name
    return name or None


def family_for(model_id: str, rules: Mapping[str, Any]) -> Tuple[int, str]:
    """(rank in policy families, family key). Unknown families rank after known ones."""
    families = rules.get("families", [])
    for rank, family in enumerate(families):
        if model_id == family or model_id.startswith(family + "-"):
            return rank, family
    words = []
    for tok in split_qualifiers(model_id)[0].split("-"):
        if VERSION.match(tok):
            break
        words.append(tok)
    return len(families), "-".join(words)


def version_of(model_id: str, rules: Mapping[str, Any]) -> Tuple[int, ...]:
    numbers: List[int] = []
    for tok in _strip_prefix(split_qualifiers(model_id)[0], rules).split("-"):
        if VERSION.match(tok):
            numbers += [int(n) for n in tok.split(".")]
        elif numbers:
            break
    return tuple(numbers)


def sort_key_for(model_id: str, rules: Mapping[str, Any]) -> Tuple[Any, ...]:
    rank, family = family_for(model_id, rules)
    return rank, family, version_of(model_id, rules), model_id


def display_name_for(row: Mapping[str, Any], rules: Mapping[str, Any]) -> Optional[str]:
    """Name to write, or None to leave OpenCodex's own name (provider name or raw slug).

    Priority: policy family rule -> provider displayName -> generic humanizer -> raw id.
    """
    model_id = row["id"]
    known = family_for(model_id, rules)[0] < len(rules.get("families", []))
    provider_named = row.get("displayNameSource") == "provider" and row.get("displayName")
    if not known and provider_named:
        return None
    return humanize(model_id, rules)


def build_display_names(rows: Sequence[Mapping[str, Any]], rules: Mapping[str, Any],
                        existing: Mapping[str, str]) -> Dict[str, str]:
    """Existing names are kept as-is (they may be user edits); only unnamed ids are filled."""
    names = dict(existing)
    generated = {r["id"]: n for r in rows if r["id"] not in existing
                 for n in [display_name_for(r, rules)] if n}
    shown = Counter(names.get(r["id"]) or generated.get(r["id"]) or r.get("displayName") or r["id"]
                    for r in rows)
    for model_id, name in generated.items():
        qualifiers = split_qualifiers(model_id)[1]
        if shown[name] > 1 and qualifiers:
            # Two ids would read the same; keep what was stripped so they stay distinct.
            name += " " + " ".join(q if DATE.match(q) else q.capitalize() for q in qualifiers)
        names[model_id] = name
    return names


def _slug(row: Mapping[str, Any], provider: str) -> str:
    return row.get("namespaced") or f"{provider}/{row['id']}"


def build_desired_config(cfg: Mapping[str, Any], lives: Mapping[str, Sequence[Mapping[str, Any]]],
                         policy: Mapping[str, Any]) -> Dict[str, Any]:
    """Config paths whose value differs from what the picker policy wants."""
    changes: Dict[str, Any] = {}
    head: List[str] = []
    for rules in policy["providers"]:
        provider = rules["name"]
        if provider not in lives:
            continue
        rows = lives[provider]
        path = f"providers.{provider}.modelDisplayNames"
        existing = read_path(cfg, path) or {}
        names = build_display_names(rows, rules, existing)
        if names != existing:
            changes[path] = names
        head += [_slug(r, provider) for r in sorted(rows, key=lambda r: sort_key_for(r["id"], rules))]
    # Unmanaged entries (other providers, bare native ids a user added) keep their order after ours.
    current = cfg.get("modelPickerOrder") or []
    tail = [s for s in current if "/" not in s or s.split("/", 1)[0] not in lives]
    order = head + [s for s in tail if s not in head]
    if order != current:
        changes["modelPickerOrder"] = order
    return changes


def missing_providers(cfg: Mapping[str, Any], policy: Mapping[str, Any]) -> List[str]:
    configured = cfg.get("providers") or {}
    return [r["name"] for r in policy["providers"] if r["name"] not in configured]


def bootstrap_changes(cfg: Mapping[str, Any], lives: Mapping[str, Sequence[Mapping[str, Any]]],
                      policy: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Policy settings plus the one-way legacy migration.

    A static selectedModels allowlist hides every future model. It is replaced by
    disabledModels entries for the models it hid that OpenCodex already knew about, so
    today's picker looks the same while later arrivals show up. disabledModels only grows.
    """
    changes = {path: value for path, value in policy.get("config", {}).items()
               if read_path(cfg, path) != value}
    disabled = list(cfg.get("disabledModels") or [])
    wanted = list(policy.get("ensureDisabled", []))
    unsets = []
    for provider, rows in lives.items():
        selected = read_path(cfg, f"providers.{provider}.selectedModels")
        if not selected:
            continue
        keep = set(selected) | {f"{provider}/{m}" for m in selected}
        baseline = read_path(cfg, f"modelDiscovery.knownModels.{provider}.ids")
        for row in rows:
            hidden = row["id"] not in keep and _slug(row, provider) not in keep
            known = baseline is None or row["id"] in baseline
            if hidden and known:
                wanted.append(_slug(row, provider))
        unsets.append(f"providers.{provider}.selectedModels")
    added = [s for s in dict.fromkeys(wanted) if s not in disabled]
    if added:
        changes["disabledModels"] = disabled + added
    return changes, unsets


def validate_rows(provider: str, rows: Any) -> List[Dict[str, Any]]:
    if not isinstance(rows, list):
        raise OcxError(f"ocx models live --provider {provider}: expected a JSON list")
    valid = [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)
             and r.get("provider", provider) == provider]
    if not valid:
        # An empty roster is far more likely a failed discovery than a provider with no models.
        raise OcxError(f"ocx models live --provider {provider}: no models returned")
    return valid


# ---------------------------------------------------------------- OpenCodex I/O

class OcxError(RuntimeError):
    pass


class Ocx:
    def __init__(self, exe: str):
        self.exe = exe

    def _run(self, *args: str, timeout: int = 60) -> str:
        try:
            result = subprocess.run([self.exe, *args], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise OcxError(f"ocx {' '.join(args[:3])}: timed out") from error
        if result.returncode != 0:
            raise OcxError(f"ocx {' '.join(args[:3])}: {(result.stderr or result.stdout).strip()}")
        return result.stdout

    def _json(self, *args: str) -> Any:
        try:
            return json.loads(self._run(*args))
        except json.JSONDecodeError as error:
            raise OcxError(f"ocx {' '.join(args[:3])}: output is not JSON") from error

    def config(self) -> Dict[str, Any]:
        return self._json("config", "show")

    def live(self, provider: str) -> Any:
        return self._json("models", "live", "--provider", provider, "--json")

    def set(self, path: str, value: Any) -> None:
        self._run("config", "set", path, json.dumps(value, ensure_ascii=False))

    def unset(self, path: str) -> None:
        self._run("config", "unset", path)

    def sync(self) -> str:
        return self._run("sync", timeout=300)


def main(argv: Optional[Sequence[str]] = None, ocx: Any = None,
         policy: Optional[Mapping[str, Any]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--bootstrap", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    policy = policy or json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    if ocx is None:
        exe = shutil.which("ocx")
        if exe is None:
            print("ocx command not found", file=sys.stderr)
            return 127
        ocx = Ocx(exe)

    # Read everything first. Any failure here exits before a single write.
    try:
        cfg = ocx.config()
        missing = missing_providers(cfg, policy)
        if args.bootstrap and missing:
            print("Missing providers: " + ", ".join(missing), file=sys.stderr)
            print("Log in first, then re-run:", file=sys.stderr)
            for provider in missing:
                print(f"  ocx login {provider}", file=sys.stderr)
            return 1
        lives = {r["name"]: validate_rows(r["name"], ocx.live(r["name"]))
                 for r in policy["providers"] if r["name"] not in missing}
    except OcxError as error:
        print(f"reconcile skipped, config untouched: {error}", file=sys.stderr)
        return 1

    changes = build_desired_config(cfg, lives, policy)
    if args.check:
        for path in changes:
            print(f"out of date: {path}", file=sys.stderr)
        print("picker names/order up to date" if not changes else "run reconcile-models.py")
        return 1 if changes else 0

    unsets: List[str] = []
    if args.bootstrap:
        settings, unsets = bootstrap_changes(cfg, lives, policy)
        changes = {**settings, **changes}
    else:
        for provider in lives:
            if read_path(cfg, f"providers.{provider}.selectedModels"):
                print(f"note: providers.{provider}.selectedModels allowlist hides new models; "
                      "./apply.sh migrates it to disabledModels", file=sys.stderr)

    try:
        for path, value in changes.items():
            ocx.set(path, value)
            print(f"set {path}")
        for path in unsets:
            ocx.unset(path)
            print(f"unset {path}")
        if changes or unsets or args.bootstrap:
            print(ocx.sync().strip())
        else:
            print("no changes")
    except OcxError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
