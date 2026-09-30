# ocx-extensions

A thin layer on top of [OpenCodex](https://github.com/lidge-jun/opencodex) that
does three things on any machine:

1. Shows OpenCodex's long model ids in the Codex desktop picker under short,
   readable names (`anthropic/claude-opus-5-5` → `Opus 5.5`).
2. Keeps the picker in a stable provider/family order, while you hide the models
   you don't want with OpenCodex's own model toggle.
3. Installs the `$ocx-quota` skill, which prints live OpenAI / Anthropic / Google
   Antigravity quota gauges.

<table>
  <tr>
    <th>Model picker</th>
    <th>Quota gauges</th>
  </tr>
  <tr>
    <td><img src="docs/model-picker.png" alt="Codex model picker with the restored order" height="300"></td>
    <td><img src="docs/quota-bars.png" alt="ocx-quota gauge output" height="300"></td>
  </tr>
</table>

## Who owns what

| Concern | Owner | Where it lives |
| --- | --- | --- |
| Which models exist | OpenCodex live discovery | provider `/models` calls |
| Which models are hidden | You, through OpenCodex | `disabledModels` in `~/.opencodex/config.json` |
| Readable names and picker order | this repo | rules in `policy.json`, written to `modelDisplayNames` / `modelPickerOrder` |
| Quota display | this repo | `skills/ocx-quota` |

This repo stores no model list. `policy.json` holds rules (family order, name
prefixes) and a few operator settings; the concrete ids always come from the live
catalog.

## How a new model shows up

```
provider releases claude-opus-5-6
→ OpenCodex catalog auto-refresh (every 15 min) adds it to the Codex catalog, visible
→ scripts/reconcile-models.py names it "Opus 5.6" and slots it after "Opus 5.5"
→ quit and reopen Codex: the picker shows it
```

New models are visible by default (`modelDiscovery.newModelPolicy = "on"`). Older
generations stay; nothing is hidden because something newer arrived. If you don't
want a model, hide it yourself (below).

`catalogAutoRefresh` is OpenCodex's own timer; it makes a new model visible without
a manual `ocx sync`, but it does not run this repo's naming. Until
`reconcile-models.py` runs, a brand-new model appears under OpenCodex's name
(the provider's display name, or the raw `provider/model` id). Run it any time:

```bash
python3 scripts/reconcile-models.py          # names + order, then ocx sync if anything changed
python3 scripts/reconcile-models.py --check  # exit 1 if something is out of date
```

It is idempotent, so it is safe on a schedule if you want names applied without
thinking about it, e.g. cron: `*/15 * * * * cd /path/to/ocx-extensions && python3 scripts/reconcile-models.py`.

## How names are made

For each live model, first match wins:

1. **Family rule.** Ids in a family listed in `policy.json` are shortened by rule:
   the vendor prefix is dropped, version digits are joined, and date, `-thinking`,
   and tier suffixes (`-low`, `-medium`, `-high`, `-tiered`) are removed.
   `claude-haiku-4-5-20251001` → `Haiku 4.5`, `gemini-3.9-flash` → `Gemini 3.9 Flash`.
   Non-Gemini models on Antigravity get a `Google` prefix
   (`claude-opus-4-6-thinking` → `Google Opus 4.6`), because they spend the
   Antigravity pool rather than your Anthropic subscription.
2. **Provider name.** An unknown family keeps the display name the provider or
   OpenCodex already supplies.
3. **Generic humanizer.** Otherwise the same shortening applies (`claude-nova-6` → `Nova 6`).
4. **Raw id.** If the id cannot be parsed safely, OpenCodex shows it as-is.

A model is never hidden because it could not be named. If two ids would read the
same, the stripped suffix is kept (`Google Opus 4.6 Thinking`). Names already in
`modelDisplayNames` are never overwritten, so a rename you make in the OpenCodex
dashboard sticks. To regenerate one, remove it with
`ocx config unset providers.<provider>.modelDisplayNames.<model-id>` and re-run.

Order within the picker: native Codex models first in Codex's own order (this repo
never adds bare native ids to `modelPickerOrder`), then Anthropic (Fable, Opus,
Sonnet, Haiku, other families), then Google Antigravity (Gemini, Google Opus,
Google Sonnet, Google GPT-OSS, other families), each family in ascending version.
Entries for other providers you added yourself keep their order after these.

## Hiding a model

Use OpenCodex's toggle; this repo has no hide list of its own.

```bash
ocx models disable anthropic/claude-opus-5-5
ocx models enable  anthropic/claude-opus-5-5
```

or the model switches in the OpenCodex dashboard (`ocx gui`). The choice is stored
in `disabledModels` in `~/.opencodex/config.json`. Reconcile never removes
entries from it, and a disabled model keeps its slot in the order, so enabling it
again puts it back where it was.

## Setup on a new machine

### Agent-driven setup (recommended)

Paste this into a new Codex task:

> https://github.com/colosair/ocx-extensions
> Read the AGENTS.md in this repo and apply the setup to my current Codex environment.

### Manual setup

Requires OpenCodex (`ocx`, proxy running) and Python 3.

```bash
git clone https://github.com/colosair/ocx-extensions
cd ocx-extensions

ocx login anthropic
ocx login google-antigravity

./apply.sh
```

`apply.sh` stops before writing anything if a provider login is missing or live
discovery fails. Otherwise it:

- applies the settings in `policy.json` (`newModelPolicy: on`, catalog auto-refresh
  every 15 minutes, subagent models, default models);
- migrates a legacy `providers.*.selectedModels` allowlist: models it was hiding
  that OpenCodex already knew about become `disabledModels` entries, the allowlist
  is removed, and models it hid that OpenCodex had not seen yet become visible;
- names and orders the live catalog, runs `ocx sync`, installs `skills/ocx-quota`
  into `$CODEX_HOME/skills`, and checks that a second pass finds nothing to change.

It is safe to re-run. Then quit and reopen the Codex desktop app; its model list is
held in memory until it restarts.

## Quota gauges

`$ocx-quota` runs `ocx provider quota --refresh --json` and prints a gauge per
provider window. `5h` and `Weekly` are rolling windows, `Fable` is its own model
window, and `Others` is the shared Google Antigravity pool for non-Gemini models.
Providers that are not connected on the machine are simply absent. It works per
provider and window, so new model generations need no change there.

## Tests

```bash
python3 -B -m unittest discover -s scripts
python3 -B -m unittest discover -s skills/ocx-quota/scripts
```

## Troubleshooting

**`Missing providers`**: log in with the printed `ocx login` command, confirm with
`ocx provider list`, and re-run.

**`reconcile skipped, config untouched: ... Proxy is not running`**: start it with
`ocx start` and re-run.

**Picker unchanged after apply**: the desktop app was not restarted.

**The OpenCodex dashboard shows old names or toggles after apply**: `ocx config set`
writes the config file, and a running proxy keeps its in-memory copy until it
restarts. The Codex catalog written by `ocx sync` is already correct.
