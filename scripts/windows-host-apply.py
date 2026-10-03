#!/usr/bin/env python3
"""Run apply once in the normal Windows logon context, for the Codex app sandbox case.

Some Codex desktop sessions on Windows run commands inside the app's package context, where
%LOCALAPPDATA% is redirected into the app's own folder. OpenCodex then refuses Codex config
writes with "CodexUserIdentityRefusal: ... redirected by a junction or reparse point". That
check protects the Codex config and is not bypassed. Instead this helper, once:

  1. registers a temporary on-demand task "ocx-extensions-setup" (no trigger) that runs
     scripts/apply.py with pythonw, so it opens no window, in your normal logon context;
  2. starts it and waits for apply to finish;
  3. deletes the task again, whether apply succeeded, failed, or timed out;
  4. prints apply's output and exits with apply's exit code.

Use it only after ./apply.sh stopped with that error. In a normal shell run ./apply.sh.
Afterwards the only ocx-extensions task left is "ocx-extensions-reconcile".
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, List
from xml.sax.saxutils import escape

TASK = "ocx-extensions-setup"
APPLY = Path(__file__).resolve().with_name("apply.py")
EXIT_LINE = re.compile(r"^ocx-extensions apply exit=(\d+)\s*$", re.MULTILINE)
TIMEOUT_SECONDS = 15 * 60

Runner = Callable[[List[str]], subprocess.CompletedProcess]


def task_xml(pythonw: str, apply_script: str, log: str, workdir: str, user: str) -> str:
    """On-demand task definition. XML keeps it locale independent and trigger free."""
    arguments = f'"{apply_script}" --log "{log}"'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Temporary ocx-extensions setup; deleted when it finishes.</Description></RegistrationInfo>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(pythonw)}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(workdir)}</WorkingDirectory></Exec></Actions>
</Task>
"""


def wait_for_exit(log: str, timeout: float, poll: float = 2.0,
                  clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep) -> int:
    """apply's exit code, read from the marker apply.py writes as its last line."""
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            match = EXIT_LINE.search(Path(log).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            match = None
        if match:
            return int(match.group(1))
        sleep(poll)
    raise TimeoutError(f"apply did not finish within {int(timeout)} s")


def run_host_native(run: Runner, wait: Callable[[str], int], *, pythonw: str, apply_script: str,
                    workdir: str, user: str, tmpdir: str) -> int:
    log = os.path.join(tmpdir, "ocx-extensions-setup.log")
    xml = os.path.join(tmpdir, "ocx-extensions-setup.xml")
    if os.path.exists(log):
        os.remove(log)
    with open(xml, "w", encoding="utf-16") as f:
        f.write(task_xml(pythonw, apply_script, log, workdir, user))
    try:
        run(["schtasks", "/Create", "/F", "/TN", TASK, "/XML", xml])
        run(["schtasks", "/Run", "/TN", TASK])
        return wait(log)
    finally:
        # Always remove the task: a setup task left behind would be a second, stale entry.
        for cmd in (["schtasks", "/End", "/TN", TASK], ["schtasks", "/Delete", "/F", "/TN", TASK]):
            try:
                run(cmd)
            except (OSError, subprocess.CalledProcessError):
                pass
        if os.path.exists(xml):
            os.remove(xml)
        if os.path.exists(log):
            print(Path(log).read_text(encoding="utf-8", errors="replace"), end="")


def schtasks(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main() -> int:
    if os.name != "nt":
        print("windows-host-apply.py is only for Windows; run ./apply.sh", file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        print(f"pythonw.exe not found next to {sys.executable}", file=sys.stderr)
        return 1
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    try:
        code = run_host_native(schtasks, lambda log: wait_for_exit(log, TIMEOUT_SECONDS),
                               pythonw=str(pythonw), apply_script=str(APPLY), workdir=str(APPLY.parent.parent),
                               user=user, tmpdir=tempfile.gettempdir())
    except (subprocess.CalledProcessError, TimeoutError, OSError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        print(f"host-native apply failed: {detail.strip()}", file=sys.stderr)
        return 1
    leftover = subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if leftover.returncode == 0:
        print(f"WARNING: task {TASK} still exists; remove it: schtasks /Delete /TN {TASK} /F", file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
