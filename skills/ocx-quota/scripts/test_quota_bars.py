"""Minimal regression tests for quota-bar normalization and formatting."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).with_name("quota-bars.py")
SPEC = importlib.util.spec_from_file_location("quota_bars", SCRIPT)
quota_bars = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = quota_bars
SPEC.loader.exec_module(quota_bars)


class QuotaBarsTest(unittest.TestCase):
    def test_profiles_map_and_order_provider_windows(self):
        reports = [
            {
                "provider": "google-antigravity",
                "quota": {"customWindows": [
                    {"label": "Cla", "percent": 10, "resetAt": 1},
                    {"label": "Gem", "percent": 20, "resetAt": 1},
                    {"label": "Gem (Weekly)", "percent": 30, "resetAt": 1},
                    {"label": "Cla (Weekly)", "percent": 40, "resetAt": 1},
                ]},
            },
            {
                "provider": "openai",
                "quota": {"fiveHourPercent": 25, "weeklyPercent": 40},
                "aggregation": {"currentAccount": {"quota": {
                    "fiveHourResetAt": 1, "weeklyResetAt": 1,
                }}},
            },
        ]

        sections = quota_bars.build_sections(reports)

        self.assertEqual([section.title for section in sections], ["OpenAI (Codex)", "Google Antigravity"])
        self.assertEqual(
            [window.label for window in sections[1].windows],
            ["Gemini 5h", "Gemini Weekly", "Others 5h", "Others Weekly"],
        )

    def test_reset_text_adds_date_only_after_today(self):
        now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)

        self.assertEqual(quota_bars.reset_text(datetime(2026, 9, 13, 19, tzinfo=timezone.utc).timestamp(), now), "오후 7:00 초기화")
        self.assertEqual(quota_bars.reset_text(datetime(2026, 9, 14, 1, tzinfo=timezone.utc).timestamp(), now), "9월 14일 오전 1:00 초기화")

    def test_missing_percent_is_not_rendered_as_full_quota(self):
        window = quota_bars.QuotaWindow("5h", None, None)

        self.assertIn("정보 없음", quota_bars.render_window(window))
        self.assertNotIn("100% 남음", quota_bars.render_window(window))

    def test_unknown_windows_and_model_fields_still_render(self):
        # Quota is keyed by provider/window, so a new model generation needs no code change here.
        reports = [{
            "provider": "google-antigravity",
            "models": ["gemini-9-flash", "claude-nova-6"],
            "quota": {"customWindows": [
                {"label": "Nova", "percent": 50, "resetAt": 1},
                {"label": "Gem", "percent": 20, "resetAt": 1},
            ]},
        }]

        section = quota_bars.build_sections(reports)[0]

        self.assertEqual([window.label for window in section.windows], ["Gemini 5h", "Nova"])
        self.assertIn("50% 남음", quota_bars.render_sections([section]))


class Utf8OutputTest(unittest.TestCase):
    """The gauge must print on a cp949 (Korean Windows) stream without python -X utf8."""

    REPORTS = [{
        "provider": "anthropic",
        "quota": {"fiveHourPercent": 60, "weeklyPercent": None, "fiveHourResetAt": None},
    }]

    def run_main_with_legacy_encoding(self, reports):
        driver = (
            "import importlib.util, json, sys\n"
            f"spec = importlib.util.spec_from_file_location('qb', {str(SCRIPT)!r})\n"
            "qb = importlib.util.module_from_spec(spec); sys.modules['qb'] = qb; spec.loader.exec_module(qb)\n"
            "qb.shutil.which = lambda name: 'ocx'\n"
            f"qb.fetch_reports = lambda ocx: json.loads({json.dumps(json.dumps(reports))})\n"
            "raise SystemExit(qb.main())\n"
        )
        env = {**os.environ, "PYTHONIOENCODING": "cp949", "PYTHONUTF8": "0"}
        return subprocess.run([sys.executable, "-c", driver], capture_output=True, env=env, timeout=60)

    def test_gauge_prints_as_utf8_on_a_cp949_stream(self):
        result = self.run_main_with_legacy_encoding(self.REPORTS)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        text = result.stdout.decode("utf-8")
        for expected in ("[████████░░░░░░░░░░░░]  40% 남음", "████████░░", "남음", "초기화", "정보 없음"):
            self.assertIn(expected, text)

    def test_ocx_output_is_decoded_as_utf8(self):
        payload = json.dumps({"reports": [{"provider": "google-antigravity", "quota": {
            "customWindows": [{"label": "남은 창", "percent": 20, "resetAt": 1}]}}]}, ensure_ascii=False)
        with tempfile.TemporaryDirectory(prefix="fake ocx ") as tmp:
            emit = Path(tmp, "emit.py")
            emit.write_text(f"import sys\nsys.stdout.buffer.write({payload.encode('utf-8')!r})\n", encoding="utf-8")
            if os.name == "nt":
                fake = Path(tmp, "ocx.cmd")
                fake.write_text(f'@"{sys.executable}" "%~dp0emit.py" %*\r\n', encoding="utf-8")
            else:
                fake = Path(tmp, "ocx")
                fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{emit}" "$@"\n', encoding="utf-8")
                fake.chmod(0o755)
            reports = quota_bars.fetch_reports(str(fake))
        self.assertEqual(reports[0]["quota"]["customWindows"][0]["label"], "남은 창")


if __name__ == "__main__":
    unittest.main()
