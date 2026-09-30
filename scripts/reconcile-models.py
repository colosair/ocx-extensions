#!/usr/bin/env python3
"""Refine the OpenCodex model catalog for the Codex picker.

OpenCodex owns the model inventory (live discovery) and visibility (disabledModels).
This script owns two derived outputs, recomputed from the live catalog every run:
  - providers.<managed>.modelDisplayNames  (short names for the policy.json providers)
  - modelPickerOrder                       (every live routed model; never bare native ids)
It never enables a model and never writes disabledModels outside --bootstrap, which
only appends (legacy selectedModels migration and policy ensureDisabled).

  reconcile-models.py              recompute names + order, ocx sync when changed
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


def build_display_names(rows: Sequence[Mapping[str, Any]], rules: Mapping[str, Any]) -> Dict[str, str]:
    """The complete override map for one provider. Rows left to OpenCodex are not listed."""
    generated = {r["id"]: n for r in rows for n in [display_name_for(r, rules)] if n}
    shown = Counter(generated.get(r["id"])
                    or (r.get("displayNameSource") == "provider" and r.get("displayName"))
                    or r["id"] for r in rows)
    names = {}
    for model_id, name in generated.items():
        qualifiers = split_qualifiers(model_id)[1]
        if shown[name] > 1 and qualifiers:
            # Two ids would read the same; keep what was stripped so they stay distinct.
            name += " " + " ".join(q if DATE.match(q) else q.capitalize() for q in qualifiers)
        names[model_id] = name
    return names


def _slug(row: Mapping[str, Any], provider: str) -> str:
    return row.get("namespaced") or f"{provider}/{row['id']}"


def routed_rows(rows: Any) -> List[Dict[str, Any]]:
    """Routed rows of the full live catalog. Native rows are Codex's to order."""
    if not isinstance(rows, list):
        raise OcxError("ocx models live: expected a JSON list")
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)
            and isinstance(r.get("provider"), str) and not r.get("native")
            and "/" in _slug(r, r["provider"])]


def managed_rows(routed: Sequence[Mapping[str, Any]], providers: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
    lives = {p: [dict(r) for r in routed if r["provider"] == p] for p in providers}
    for provider, rows in lives.items():
        if not rows:
            # An empty roster is far more likely a failed discovery than a provider with no models.
            raise OcxError(f"ocx models live: no models returned for {provider}")
    return lives


def build_desired_config(cfg: Mapping[str, Any], routed: Sequence[Mapping[str, Any]],
                         policy: Mapping[str, Any]) -> Dict[str, Any]:
    """Config paths whose value differs from what the picker policy wants (None = unset)."""
    changes: Dict[str, Any] = {}
    head: List[str] = []
    managed = set()
    for rules in policy["providers"]:
        provider = rules["name"]
        rows = [r for r in routed if r["provider"] == provider]
        if not rows:
            continue
        managed.add(provider)
        path = f"providers.{provider}.modelDisplayNames"
        names = build_display_names(rows, rules)
        if names != (read_path(cfg, path) or {}):
            changes[path] = names or None
        head += [_slug(r, provider) for r in sorted(rows, key=lambda r: sort_key_for(r["id"], rules))]

    # Every other routed model follows, so none is left outside the order to jump ahead of
    # the managed band. Saved relative order wins; new models go to the end of their provider
    # group, new providers after known ones. Saved entries not live right now are kept, since
    # their provider may just be failing discovery. Bare native ids are dropped.
    current = cfg.get("modelPickerOrder") or []
    position = {slug: i for i, slug in reversed(list(enumerate(current)))}
    provider_position: Dict[str, int] = {}
    for slug in current:
        provider_position.setdefault(slug.split("/", 1)[0], position[slug])
    others = dict.fromkeys(
        [s for s in current if "/" in s and s.split("/", 1)[0] not in managed]
        + [_slug(r, r["provider"]) for r in routed if r["provider"] not in managed])
    missing = len(current)
    tail = sorted(others, key=lambda s: (provider_position.get(s.split("/", 1)[0], missing),
                                         s.split("/", 1)[0], position.get(s, missing), s))
    order = head + tail
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


# ---------------------------------------------------------------- OpenCodex I/O

class OcxError(RuntimeError):
    pass


class Ocx:
    def __init__(self, exe: str):
        self.exe = exe

    def _run(self, *args: str, timeout: int = 60) -> str:
        try:
            # CREATE_NO_WINDOW: a scheduled run under pythonw must not flash a console per call.
            result = subprocess.run([self.exe, *args], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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

    def live_all(self) -> Any:
        return self._json("models", "live", "--json")

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
        routed = routed_rows(ocx.live_all())
        lives = managed_rows(routed, [r["name"] for r in policy["providers"] if r["name"] not in missing])
    except OcxError as error:
        print(f"reconcile skipped, config untouched: {error}", file=sys.stderr)
        return 1

    changes = build_desired_config(cfg, routed, policy)
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
            if value is None:
                ocx.unset(path)
                print(f"unset {path}")
            else:
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
