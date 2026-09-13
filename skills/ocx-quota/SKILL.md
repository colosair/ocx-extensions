---
name: ocx-quota
description: Display current OpenCodex provider quotas as compact, aligned text gauge bars. Use when the user asks to view OpenAI, Anthropic, or Google Antigravity usage in the agreed terminal-style format.
---

# Opencodex Quota Bars

Run `python3 scripts/quota-bars.py` to fetch live quota data with `ocx provider quota --json` and print it.

Paste the command output into the chat in a fenced `text` block. Preserve its format:

- English provider and window labels.
- `█` / `░` gauge bar, then Korean remaining-percentage text and reset time in parentheses; do not show used percentage.
- Show only the time for a same-day reset; include `M월 D일` when the reset date differs from today.
- Google Antigravity labels its non-Gemini shared Claude/GPT pool as `Others 5h` and `Others Weekly`.
- When OpenCodex omits a quota percentage, show `정보 없음`; never infer it as 100% remaining.

Do not change OpenCodex configuration, provider selection, or model visibility for a quota-display request. If the user requests a format revision, update `scripts/quota-bars.py` and run it once to verify the rendered text.
