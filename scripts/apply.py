#!/usr/bin/env python3
"""Bootstrap this machine: picker policy on top of OpenCodex's live catalog + quota skill.

apply.sh runs this; scripts/windows-host-apply.py runs it under pythonw. Every step is
idempotent and never re-enables a model the user disabled:
  1. reconcile-models.py --bootstrap  provider check, policy settings, legacy migration,
                                      generated selectedModels / names / order, ocx sync
  2. skills/ into $CODEX_HOME/skills
  3. the single 15-minute reconcile scheduler entry (a failure only warns)
  4. reconcile-models.py --check      a second pass must find nothing to change
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import shutil
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Optional, Sequence

REPO = Path(__file__).resolve().parent.parent
EXIT_MARKER = "ocx-extensions apply exit="


def load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def tree_digest(root: str) -> str:
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


def install_skills(repo: Path, codex_home: str) -> None:
    """Copy skills/*. A changed existing copy is moved to $CODEX_HOME/skill-backups, outside
    the skills folder, so Codex does not load the backup as a second copy of the skill."""
    skills_src = repo / "skills"
    skills_dst = os.path.join(codex_home, "skills")
    backups = os.path.join(codex_home, "skill-backups")
    os.makedirs(skills_dst, exist_ok=True)
    for name in sorted(os.listdir(skills_src)):
        src, dst = os.path.join(skills_src, name), os.path.join(skills_dst, name)
        if not os.path.isdir(src):
            continue
        if os.path.exists(dst):
            if tree_digest(src) == tree_digest(dst):
                print(f"skill {name} already current")
                continue
            os.makedirs(backups, exist_ok=True)
            backup = os.path.join(backups, f"{name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.move(dst, backup)
            print(f"existing skill moved to {backup}")
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        print(f"installed skill {name}")


def service_note(rm: ModuleType, exe: str) -> None:
    """Advise, never act: the proxy service belongs to OpenCodex (ocx service)."""
    try:
        status = rm.Ocx(exe)._run("service", "status", timeout=60)
    except rm.OcxError as error:
        status = str(error)
    if "not installed" in status.lower() or "service installed" not in status.lower():
        print("note: the OpenCodex background service is not installed; run 'ocx service' so the "
              "proxy, and with it the 15-minute reconcile, survives a reboot", file=sys.stderr)


def apply() -> int:
    exe = shutil.which("ocx")
    if exe is None:
        print("OpenCodex (ocx) is required but was not found.", file=sys.stderr)
        print("Please install OpenCodex first, then re-run this script.", file=sys.stderr)
        return 1
    rm = load("reconcile_models", "reconcile-models.py")
    sched = load("install_auto_reconcile", "install-auto-reconcile.py")
    service_note(rm, exe)

    code = rm.main(["--bootstrap"])
    if code:
        return code
    codex_home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    install_skills(REPO, codex_home)
    if sched.main():
        print("WARNING: Automatic model reconciliation was not registered.", file=sys.stderr)
        print("Run scripts/reconcile-models.py manually or fix the scheduler setup.", file=sys.stderr)
    code = rm.main(["--check"])
    if code:
        return code
    print()
    print("Done. Quit and reopen the Codex desktop app so its model picker reloads.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", help="write all output to this file (used under pythonw)")
    args = parser.parse_args(argv)
    if not args.log:
        return apply()
    with open(args.log, "w", encoding="utf-8", buffering=1) as log:
        sys.stdout = sys.stderr = log
        try:
            code = apply()
        except Exception as error:  # the caller only sees this file; record why it stopped
            print(f"apply failed: {error!r}")
            code = 1
        print(f"{EXIT_MARKER}{code}")
        return code


if __name__ == "__main__":
    raise SystemExit(main())
