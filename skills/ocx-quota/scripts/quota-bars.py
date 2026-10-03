#!/usr/bin/env python3
"""Render OpenCodex quota reports in the user's compact gauge format."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path as FilePath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union


Path = Tuple[str, ...]


@dataclass(frozen=True)
class WindowSpec:
    label: str
    percent_path: Path
    reset_path: Path


@dataclass(frozen=True)
class ProviderSpec:
    title: str
    windows: Tuple[WindowSpec, ...] = ()
    custom_labels: Tuple[Tuple[str, str], ...] = ()
    custom_order: Tuple[str, ...] = ()


# Add a provider by declaring source paths and labels here; rendering stays generic.
PROFILES: Mapping[str, ProviderSpec] = {
    "openai": ProviderSpec(
        title="OpenAI (Codex)",
        windows=(
            WindowSpec("5h", ("quota", "fiveHourPercent"), ("aggregation", "currentAccount", "quota", "fiveHourResetAt")),
            WindowSpec("Weekly", ("quota", "weeklyPercent"), ("aggregation", "currentAccount", "quota", "weeklyResetAt")),
        ),
    ),
    "anthropic": ProviderSpec(
        title="Anthropic Claude",
        windows=(
            WindowSpec("5h", ("quota", "fiveHourPercent"), ("quota", "fiveHourResetAt")),
            WindowSpec("Weekly", ("quota", "weeklyPercent"), ("quota", "weeklyResetAt")),
        ),
    ),
    "google-antigravity": ProviderSpec(
        title="Google Antigravity",
        custom_labels=(
            ("Gem", "Gemini 5h"),
            ("Gem (Weekly)", "Gemini Weekly"),
            ("Cla", "Others 5h"),
            ("Cla (Weekly)", "Others Weekly"),
        ),
        custom_order=("Gem", "Gem (Weekly)", "Cla", "Cla (Weekly)"),
    ),
}


@dataclass(frozen=True)
class QuotaWindow:
    label: str
    used_percent: Optional[int]
    reset_at: object


@dataclass(frozen=True)
class Section:
    title: str
    windows: Tuple[QuotaWindow, ...]


def read_path(data: Mapping[str, Any], path: Path) -> object:
    value: object = data
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def used_percentage(value: object) -> Optional[int]:
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return None


def reset_text(value: object, now: Optional[datetime] = None) -> str:
    now = now or datetime.now().astimezone()
    try:
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        reset = datetime.fromtimestamp(timestamp, tz=now.tzinfo)
    except (TypeError, ValueError, OSError, OverflowError):
        return "초기화 정보 없음"

    period = "오전" if reset.hour < 12 else "오후"
    hour = reset.hour % 12 or 12
    when = f"{period} {hour}:{reset.minute:02d}"
    if reset.date() != now.date():
        when = f"{reset.month}월 {reset.day}일 {when}"
    return f"{when} 초기화"


def custom_windows(report: Mapping[str, Any], profile: ProviderSpec) -> Tuple[QuotaWindow, ...]:
    quota = report.get("quota")
    if not isinstance(quota, Mapping):
        return ()
    raw_windows = quota.get("customWindows")
    if not isinstance(raw_windows, list):
        return ()

    by_label = {
        window.get("label"): window
        for window in raw_windows
        if isinstance(window, Mapping) and isinstance(window.get("label"), str)
    }
    labels = dict(profile.custom_labels)
    ordered = [label for label in profile.custom_order if label in by_label]
    ordered.extend(label for label in by_label if label not in ordered)
    return tuple(
        QuotaWindow(
            label=labels.get(raw_label, raw_label),
            used_percent=used_percentage(by_label[raw_label].get("percent")),
            reset_at=by_label[raw_label].get("resetAt"),
        )
        for raw_label in ordered
    )


def normalize_report(report: Mapping[str, Any], profile: ProviderSpec) -> Section:
    windows = tuple(
        QuotaWindow(
            label=spec.label,
            used_percent=used_percentage(read_path(report, spec.percent_path)),
            reset_at=read_path(report, spec.reset_path),
        )
        for spec in profile.windows
    )
    return Section(profile.title, windows + custom_windows(report, profile))


def build_sections(reports: Sequence[object]) -> Tuple[Section, ...]:
    reports_by_provider = {
        report.get("provider"): report
        for report in reports
        if isinstance(report, Mapping) and isinstance(report.get("provider"), str)
    }
    return tuple(
        normalize_report(reports_by_provider[provider], profile)
        for provider, profile in PROFILES.items()
        if provider in reports_by_provider
    )


def render_window(window: QuotaWindow) -> str:
    if window.used_percent is None:
        status = "정보 없음"
    else:
        remaining = 100 - window.used_percent
        gauge = "█" * (remaining // 5) + "░" * (20 - remaining // 5)
        status = f"[{gauge}] {remaining:3}% 남음"
    return f"  {window.label:<14} {status} ({reset_text(window.reset_at)})"


def render_sections(sections: Sequence[Section]) -> str:
    return "\n\n".join(
        "\n".join([section.title, *(render_window(window) for window in section.windows)])
        for section in sections
    )


# Same hidden ocx launch as scripts/reconcile-models.py; the skill is installed on its own,
# so it keeps a copy. test_reconcile_models.py checks that the two stay identical.
NPM_SHIM_ENTRY = re.compile(r'"%dp0%\\([^"%]+\.(?:mjs|cjs|js))"', re.IGNORECASE)
CMD_UNSAFE = set('%!\r\n\0')


def npm_shim_entry(shim_text: str) -> Optional[str]:
    match = NPM_SHIM_ENTRY.search(shim_text)
    return match.group(1) if match else None


def cmd_quote(arg: str) -> str:
    if CMD_UNSAFE & set(arg):
        raise RuntimeError(f"argument cannot be passed through cmd.exe safely: {arg!r}")
    arg = re.sub(r'(\\+)(?="|$)', lambda m: m.group(1) * 2, arg)
    return '"' + arg.replace('"', '""') + '"'


def build_ocx_command(exe: str, args: Sequence[str], *, windows: bool = os.name == "nt",
                      read_text: Callable[[str], str] = lambda p: FilePath(p).read_text(encoding="utf-8", errors="replace"),
                      exists: Callable[[str], bool] = os.path.exists,
                      which: Callable[[str], Optional[str]] = shutil.which,
                      comspec: Optional[str] = None) -> Union[List[str], str]:
    """argv that runs ocx without a console; an npm ocx.cmd shim is resolved to node + script."""
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
    extra: Dict[str, Any] = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=timeout, **extra)


def use_utf8_output() -> None:
    """Print UTF-8 even where the console or pipe defaults to a legacy code page (cp949).

    Without this, Windows needs python -X utf8 to print the gauge characters at all.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        if encoding == "utf8" or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # an already-written or detached stream keeps its encoding


def fetch_reports(ocx: str) -> Sequence[object]:
    try:
        result = run_hidden(build_ocx_command(ocx, ["provider", "quota", "--refresh", "--json"]), timeout=15)
        if result.returncode != 0:
            raise RuntimeError("OpenCodex 쿼터 조회에 실패했습니다")
        payload = json.loads(result.stdout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("OpenCodex 쿼터 조회 시간이 초과되었습니다") from error
    except OSError as error:
        raise RuntimeError("OpenCodex 쿼터 조회에 실패했습니다") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("OpenCodex 쿼터 응답이 JSON 형식이 아닙니다") from error

    reports = payload.get("reports") if isinstance(payload, Mapping) else None
    if not isinstance(reports, list):
        raise RuntimeError("OpenCodex 쿼터 응답에 reports 목록이 없습니다")
    return reports


def main() -> int:
    use_utf8_output()
    ocx = shutil.which("ocx")
    if ocx is None:
        print("ocx command not found", file=sys.stderr)
        return 127
    try:
        output = render_sections(build_sections(fetch_reports(ocx)))
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
