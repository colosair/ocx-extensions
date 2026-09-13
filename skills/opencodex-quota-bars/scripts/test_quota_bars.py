"""Minimal regression tests for quota-bar normalization and formatting."""

import importlib.util
import sys
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


if __name__ == "__main__":
    unittest.main()
