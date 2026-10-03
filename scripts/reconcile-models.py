#!/usr/bin/env python3
"""Curate the OpenCodex model catalog for the Codex picker.

OpenCodex owns the model inventory (live discovery) and the models the user hid
(disabledModels). For the providers in policy.json this script owns three derived
outputs, recomputed from the live catalog on every run:
  - providers.<managed>.selectedModels     the newest maxGenerationsPerFamily generations
                                           of each model family, newest first
  - providers.<managed>.modelDisplayNames  short names for those models
  - modelPickerOrder                       that selection, then every other routed model
                                           (never bare native ids)
Older generations stay discovered and routable; they are only left out of the picker.
It never enables a model and never writes disabledModels outside --bootstrap, which only
appends (legacy static selectedModels migration and policy ensureDisabled).

  reconcile-models.py              recompute, ocx sync when something changed
  reconcile-models.py --bootstrap  also: provider check, policy settings, legacy migration
  reconcile-models.py --check      exit 1 if any generated value is out of date
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

POLICY_PATH = Path(__file__).resolve().parent.parent / "policy.json"

DATE = re.compile(r"^\d{8}$")
# Capability/tier suffixes: one model generation offered at several settings.
QUALIFIERS = {"thinking", "minimal", "low", "medium", "mid", "high", "xhigh", "extra", "tiered"}
# Release tags: kept in the name, but still a variant of the same generation.
RELEASE_TAGS = {"preview", "exp", "latest"}
# Representative for a generation offered only with capability suffixes, best first.
CAPABILITY_PREFERENCE = ("thinking", "high", "medium", "tiered", "xhigh", "mid", "low", "minimal", "extra")
VALID_ID = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
VERSION = re.compile(r"^\d+(?:\.\d+)*$")
SIZE = re.compile(r"^\d+(?:\.\d+)?[bkm]$")
ACRONYMS = {"gpt", "oss"}
# OpenCodex refusing Codex writes from the Codex app's Windows package context. The first two
# fail the command; the last is printed by a sync that exits 0 without writing the catalog.
REFUSAL = ("CodexUserIdentityRefusal", "junction or reparse point", "unsafe coordinator namespace")


# ---------------------------------------------------------------- pure logic

def read_path(data: Mapping[str, Any], path: str) -> Any:
    node: Any = data
    for key in path.split("."):
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def split_qualifiers(model_id: str) -> Tuple[str, List[str]]:
    """'claude-opus-4-6-thinking' -> ('claude-opus-4-6', ['thinking']). Used for naming."""
    tokens = model_id.split("-")
    tail: List[str] = []
    while len(tokens) > 1 and (tokens[-1] in QUALIFIERS or DATE.match(tokens[-1])):
        tail.insert(0, tokens.pop())
    return "-".join(tokens), tail


def model_parts(model_id: str) -> Tuple[str, Tuple[int, ...], Tuple[str, ...]]:
    """(family, generation, variant) of an id.

    'claude-opus-4-6-thinking'  -> ('claude-opus', (4, 6), ('thinking',))
    'claude-haiku-4-5-20251001' -> ('claude-haiku', (4, 5), ('20251001',))
    'gemini-3.1-flash-image'    -> ('gemini-flash-image', (3, 1), ())
    The family is every word that is not the version, so Gemini Flash and Gemini Pro are
    separate families. Trailing zeros are dropped so '5' and '5-0' are one generation.
    """
    tokens = model_id.split("-")
    cut = next((i for i, tok in enumerate(tokens)
                if i and (tok in QUALIFIERS or tok in RELEASE_TAGS or DATE.match(tok))), len(tokens))
    words: List[str] = []
    numbers: List[int] = []
    state = "before"  # the first contiguous run of version tokens is the generation
    for tok in tokens[:cut]:
        if VERSION.match(tok) and state != "after":
            numbers += [int(n) for n in tok.split(".")]
            state = "in"
        else:
            if state == "in":
                state = "after"
            words.append(tok)
    while numbers and numbers[-1] == 0:
        numbers.pop()
    return "-".join(words), tuple(numbers), tuple(tokens[cut:])


def family_for(model_id: str) -> str:
    return model_parts(model_id)[0]


def generation_for(model_id: str) -> Tuple[int, ...]:
    return model_parts(model_id)[1]


def variant_for(model_id: str) -> Tuple[str, ...]:
    return model_parts(model_id)[2]


def family_rank(family: str, rules: Mapping[str, Any]) -> int:
    """Position of a family in policy order; the longest matching entry wins.

    'gemini-flash-image' matches its own entry before 'gemini-flash'. A family no entry
    matches ranks after every known one.
    """
    families = rules.get("families", [])
    best: Optional[int] = None
    for rank, entry in enumerate(families):
        if family == entry or family.startswith(entry + "-"):
            if best is None or len(entry) > len(families[best]):
                best = rank
    return len(families) if best is None else best


def variant_rank(variant: Sequence[str]) -> Tuple[int, int, int]:
    """Lower is preferred: plain > undated tag > dated snapshot > capability suffix."""
    if not variant:
        return 0, 0, 0
    capabilities = [t for t in variant if t in QUALIFIERS]
    if capabilities:
        best = min(CAPABILITY_PREFERENCE.index(t) if t in CAPABILITY_PREFERENCE else len(CAPABILITY_PREFERENCE)
                   for t in capabilities)
        return 3, len(variant), best
    dates = [int(t) for t in variant if DATE.match(t)]
    if dates:
        return 2, len(variant), -max(dates)  # newest snapshot first
    return 1, len(variant), 0


def preferred_variant(model_ids: Sequence[str]) -> str:
    """The one id that stands for a generation in the picker. Only live ids are candidates."""
    return min(model_ids, key=lambda model_id: (variant_rank(variant_for(model_id)), model_id))


def select_models(model_ids: Sequence[str], rules: Mapping[str, Any], limit: int) -> List[str]:
    """Newest 'limit' generations of each family, one id each, in picker order.

    Families follow policy order (unknown families after, by name); inside a family the
    newest generation comes first. Variants of one generation count once.
    """
    families: Dict[str, Dict[Tuple[int, ...], List[str]]] = {}
    for model_id in dict.fromkeys(model_ids):
        family, generation, _ = model_parts(model_id)
        families.setdefault(family, {}).setdefault(generation, []).append(model_id)
    picked: List[str] = []
    for family in sorted(families, key=lambda f: (family_rank(f, rules), f)):
        generations = sorted(families[family], reverse=True)[:limit]
        picked += [preferred_variant(families[family][g]) for g in generations]
    return picked


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


def display_name_for(row: Mapping[str, Any], rules: Mapping[str, Any]) -> Optional[str]:
    """Name to write, or None to leave OpenCodex's own name (provider name or raw slug).

    Priority: policy family rule -> provider displayName -> generic humanizer -> raw id.
    """
    model_id = row["id"]
    known = family_rank(family_for(model_id), rules) < len(rules.get("families", []))
    provider_named = row.get("displayNameSource") == "provider" and row.get("displayName")
    if not known and provider_named:
        return None
    return humanize(model_id, rules)


def build_display_names(rows: Sequence[Mapping[str, Any]], rules: Mapping[str, Any]) -> Dict[str, str]:
    """The complete override map for the given rows. Rows left to OpenCodex are not listed."""
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


def generation_limit(policy: Mapping[str, Any]) -> int:
    limit = policy.get("maxGenerationsPerFamily")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("policy.json: maxGenerationsPerFamily must be a positive integer")
    return limit


def build_desired_config(cfg: Mapping[str, Any], routed: Sequence[Mapping[str, Any]],
                         policy: Mapping[str, Any]) -> Dict[str, Any]:
    """Config paths whose value differs from what the picker policy wants (None = unset)."""
    limit = generation_limit(policy)
    changes: Dict[str, Any] = {}
    head: List[str] = []
    managed = set()
    for rules in policy["providers"]:
        provider = rules["name"]
        rows = {r["id"]: r for r in routed if r["provider"] == provider}
        if not rows:
            continue
        managed.add(provider)
        selected = select_models(list(rows), rules, limit)
        path = f"providers.{provider}.selectedModels"
        if selected != read_path(cfg, path):
            changes[path] = selected
        # Names only for the picker selection: hidden older generations need none, and a
        # user-disabled model stays selected, so it keeps its name when enabled again.
        path = f"providers.{provider}.modelDisplayNames"
        names = build_display_names([rows[i] for i in selected], rules)
        if names != (read_path(cfg, path) or {}):
            changes[path] = names or None
        head += [_slug(rows[i], provider) for i in selected]

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


def legacy_selections(cfg: Mapping[str, Any], changes: Mapping[str, Any],
                      providers: Sequence[str]) -> List[str]:
    """Managed providers whose selectedModels is a legacy static allowlist.

    Every bootstrap since the dynamic catalog sets modelDiscovery.newModelPolicy to "on" in
    the same run that writes selectedModels, while the static profile never set it. So with
    "on" present the list is this script's own (possibly one run stale) output. A list that
    already equals the generated one needs no migration either way.
    """
    if read_path(cfg, "modelDiscovery.newModelPolicy") == "on":
        return []
    return [p for p in providers
            if read_path(cfg, f"providers.{p}.selectedModels")
            and f"providers.{p}.selectedModels" in changes]


def bootstrap_changes(cfg: Mapping[str, Any], lives: Mapping[str, Sequence[Mapping[str, Any]]],
                      policy: Mapping[str, Any], legacy: Sequence[str]) -> Dict[str, Any]:
    """Policy settings plus the one-way legacy migration.

    A legacy static allowlist encoded what the user had hidden. The models it hid that
    OpenCodex already knew about become disabledModels entries, so those choices survive;
    the list itself is then replaced by the generated selection. disabledModels only grows.
    """
    changes = {path: value for path, value in policy.get("config", {}).items()
               if read_path(cfg, path) != value}
    disabled = list(cfg.get("disabledModels") or [])
    wanted = list(policy.get("ensureDisabled", []))
    for provider in legacy:
        selected = read_path(cfg, f"providers.{provider}.selectedModels")
        keep = set(selected) | {f"{provider}/{m}" for m in selected}
        baseline = read_path(cfg, f"modelDiscovery.knownModels.{provider}.ids")
        for row in lives[provider]:
            hidden = row["id"] not in keep and _slug(row, provider) not in keep
            known = baseline is None or row["id"] in baseline
            if hidden and known:
                wanted.append(_slug(row, provider))
    added = [s for s in dict.fromkeys(wanted) if s not in disabled]
    if added:
        changes["disabledModels"] = disabled + added
    return changes


# ---------------------------------------------------------------- OpenCodex I/O

class OcxError(RuntimeError):
    pass


# The npm shim ends in: "%_prog%"  "%dp0%\node_modules\...\bin\ocx.mjs" %*
NPM_SHIM_ENTRY = re.compile(r'"%dp0%\\([^"%]+\.(?:mjs|cjs|js))"', re.IGNORECASE)
CMD_UNSAFE = set('%!\r\n\0')


def npm_shim_entry(shim_text: str) -> Optional[str]:
    """Script path, relative to the shim's folder, that an npm .cmd shim hands to node."""
    match = NPM_SHIM_ENTRY.search(shim_text)
    return match.group(1) if match else None


def cmd_quote(arg: str) -> str:
    """Quote one argument for cmd.exe /s /c and the MSVCRT parser behind it.

    Embedded quotes are doubled, which keeps cmd's quote state from flipping mid-argument,
    and backslashes before a quote are doubled for MSVCRT. % and ! cannot be escaped
    inside quotes, so such arguments are refused instead of mangled.
    """
    if CMD_UNSAFE & set(arg):
        raise OcxError(f"argument cannot be passed through cmd.exe safely: {arg!r}")
    arg = re.sub(r'(\\+)(?="|$)', lambda m: m.group(1) * 2, arg)
    return '"' + arg.replace('"', '""') + '"'


def build_ocx_command(exe: str, args: Sequence[str], *, windows: bool = os.name == "nt",
                      read_text: Callable[[str], str] = lambda p: Path(p).read_text(encoding="utf-8", errors="replace"),
                      exists: Callable[[str], bool] = os.path.exists,
                      which: Callable[[str], Optional[str]] = shutil.which,
                      comspec: Optional[str] = None) -> Union[List[str], str]:
    """argv (or, for cmd.exe, a finished command line) that runs ocx without a console.

    On Windows an npm install exposes ocx as an ocx.cmd shim. Running a .cmd means running
    cmd.exe, so the shim is resolved to the node script it wraps and node is run directly.
    Only when that fails is cmd.exe used, with every argument quoted explicitly.
    """
    if not windows or not exe.lower().endswith((".cmd", ".bat")):
        return [exe, *args]
    folder = os.path.dirname(exe)
    try:
        entry = npm_shim_entry(read_text(exe))
    except OSError:
        entry = None
    if entry:
        script = os.path.join(folder, entry)
        local_node = os.path.join(folder, "node.exe")
        node = local_node if exists(local_node) else which("node")
        if node and exists(script):
            return [node, script, *args]
    shell = comspec or os.environ.get("ComSpec") or r"C:\Windows\System32\cmd.exe"
    inner = " ".join(cmd_quote(a) for a in [exe, *args])
    return f'{cmd_quote(shell)} /d /s /c "{inner}"'


def run_hidden(command: Union[List[str], str], timeout: int) -> subprocess.CompletedProcess:
    """Run a console program with no window, UTF-8 output, and no inherited stdin."""
    extra: Dict[str, Any] = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=timeout, **extra)


class Ocx:
    def __init__(self, exe: str):
        self.exe = exe

    def _run(self, *args: str, timeout: int = 60) -> str:
        label = f"ocx {' '.join(args[:3])}"
        try:
            result = run_hidden(build_ocx_command(self.exe, args), timeout)
        except subprocess.TimeoutExpired as error:
            raise OcxError(f"{label}: timed out") from error
        except OSError as error:
            raise OcxError(f"{label}: {error}") from error
        if result.returncode != 0:
            raise OcxError(f"{label}: {(result.stderr or result.stdout).strip()}")
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


def use_utf8_output() -> None:
    """Print UTF-8 even where the console or pipe defaults to a legacy code page (cp949).

    ocx sync output contains symbols such as ⚠ that cp949 cannot encode.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        if encoding == "utf8" or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # an already-written or detached stream keeps its encoding


def refusal_hint(message: str) -> Optional[str]:
    """Next step when OpenCodex refused a write from inside the Codex app's package context."""
    if not any(marker in message for marker in REFUSAL):
        return None
    return ("OpenCodex refused to write the Codex config from this shell, which runs inside the "
            "Codex app's Windows package; the Codex catalog was not updated. Run the setup once "
            "in the normal Windows context:\n"
            "  python scripts/windows-host-apply.py")


def main(argv: Optional[Sequence[str]] = None, ocx: Any = None,
         policy: Optional[Mapping[str, Any]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--bootstrap", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    use_utf8_output()
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
    legacy = legacy_selections(cfg, changes, list(lives))
    if args.check:
        for path in changes:
            print(f"out of date: {path}", file=sys.stderr)
        print("picker selection/names/order up to date" if not changes else "run reconcile-models.py")
        return 1 if changes else 0

    if args.bootstrap:
        changes = {**bootstrap_changes(cfg, lives, policy, legacy), **changes}
    else:
        for provider in legacy:
            # Overwriting it here would lose which models the user had hidden.
            del changes[f"providers.{provider}.selectedModels"]
            print(f"note: providers.{provider}.selectedModels is a legacy static allowlist; "
                  "./apply.sh migrates it", file=sys.stderr)

    try:
        for path, value in changes.items():
            if value is None:
                ocx.unset(path)
                print(f"unset {path}")
            else:
                ocx.set(path, value)
                print(f"set {path}")
        if changes or args.bootstrap:
            synced = ocx.sync().strip()
            print(synced)
            hint = refusal_hint(synced)
            if hint:
                print(hint, file=sys.stderr)
                return 3
        else:
            print("no changes")
    except OcxError as error:
        print(str(error), file=sys.stderr)
        hint = refusal_hint(str(error))
        if hint:
            print(hint, file=sys.stderr)
            return 3
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
