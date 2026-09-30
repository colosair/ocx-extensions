#!/usr/bin/env python3
"""Run reconcile-models.py every 15 minutes with the OS scheduler.

OpenCodex's catalogAutoRefresh finds new models on the same cadence; this makes their
names and picker position follow without anyone running the script by hand.

  Windows  Task Scheduler task "ocx-extensions-reconcile" (pythonw, no console window)
  macOS    ~/Library/LaunchAgents/com.ocx-extensions.reconcile.plist
  Linux    one user crontab line tagged "# ocx-extensions-reconcile"

Re-running replaces the entry with the current repo path and Python, so there is
always exactly one. To remove it:

  Windows  schtasks /Delete /TN ocx-extensions-reconcile /F
  macOS    launchctl unload ~/Library/LaunchAgents/com.ocx-extensions.reconcile.plist
           rm ~/Library/LaunchAgents/com.ocx-extensions.reconcile.plist
  Linux    crontab -l | grep -v ocx-extensions-reconcile | crontab -
"""

from __future__ import annotations

import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

NAME = "ocx-extensions-reconcile"
LABEL = "com.ocx-extensions.reconcile"
MINUTES = 15  # OpenCodex's catalogAutoRefresh floor; a faster reconcile would find nothing new.
SCRIPT = Path(__file__).resolve().with_name("reconcile-models.py")
LOG = os.path.join(tempfile.gettempdir(), f"{NAME}.log")


def windows_python(python: str) -> str:
    """pythonw.exe next to python.exe runs without opening a console every 15 minutes."""
    windowless = Path(python).with_name("pythonw.exe")
    return str(windowless) if windowless.exists() else python


def windows_command(python: str, script: str) -> List[str]:
    # /F overwrites a task of the same name, which is what keeps re-runs to one task.
    return ["schtasks", "/Create", "/F", "/TN", NAME, "/SC", "MINUTE", "/MO", str(MINUTES),
            "/TR", f'"{python}" "{script}"']


def launchd_plist(python: str, script: str, path_env: str, log: str) -> bytes:
    # launchd starts jobs with a bare PATH; ocx and node need the installing shell's PATH.
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python, script],
        "StartInterval": MINUTES * 60,
        "EnvironmentVariables": {"PATH": path_env},
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def cron_table(existing: str, python: str, script: str, path_env: str, log: str) -> str:
    keep = [line for line in existing.splitlines() if NAME not in line]
    line = (f"*/{MINUTES} * * * * PATH={shlex.quote(path_env)} {shlex.quote(python)} "
            f"{shlex.quote(script)} >> {shlex.quote(log)} 2>&1 # {NAME}")
    return "\n".join(keep + [line]) + "\n"


def run(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=True, **kwargs)


def install() -> str:
    python, script, path_env = sys.executable, str(SCRIPT), os.environ.get("PATH", "")
    system = platform.system()
    if system == "Windows":
        python = windows_python(python)
        run(windows_command(python, script))
        return f'Task Scheduler "{NAME}" every {MINUTES} min: {python} {script}'
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)  # absent is fine
        plist.write_bytes(launchd_plist(python, script, path_env, LOG))
        run(["launchctl", "load", "-w", str(plist)])
        return f"LaunchAgent {LABEL} every {MINUTES} min: {python} {script} (log {LOG})"
    if not shutil.which("crontab"):
        raise OSError("crontab not found")
    listed = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = listed.stdout if listed.returncode == 0 else ""  # no crontab yet
    run(["crontab", "-"], input=cron_table(existing, python, script, path_env, LOG))
    return f"crontab entry {NAME} every {MINUTES} min: {python} {script} (log {LOG})"


def main() -> int:
    try:
        print(install())
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        print(f"scheduler error: {detail.strip()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
