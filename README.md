# codex-profile

Reproduces one Codex desktop setup on a new machine: the model picker list
(order, visibility, display names) and the OpenCodex quota-bar skill.

## What this repo is

The Codex model picker is built by `ocx sync` from a handful of declarative
fields in `~/.opencodex/config.json`. The files it generates —
`~/.codex/opencodex-catalog.json` and `~/.codex/models_cache.json` — are
rebuilt on every sync, so they are **not** stored here. Copying them would be
overwritten on the next sync and would carry another machine's account rows.

What travels between machines:

| File | Contents |
| --- | --- |
| `profile.json` | The config paths that define picker order, hidden models, and display names |
| `skills/opencodex-quota-bars/` | The quota-gauge skill, copied into `$CODEX_HOME/skills` |

Accounts, OAuth tokens, ports, and API keys are never stored here. Each machine
logs in on its own.

## Setup on a new machine

Requires `opencodex` (`ocx`) and `python3` already installed.

```bash
git clone <this-repo-url> ~/codex-profile
cd ~/codex-profile

ocx login anthropic            # browser OAuth, creates the anthropic provider
ocx login google-antigravity   # browser OAuth, creates the Antigravity provider

./apply.sh
```

`apply.sh` refuses to run until both providers exist, then applies every setting
through `ocx config set`, installs the skill, runs `ocx sync`, and verifies the
resulting order matches `profile.json`. It is safe to re-run.

Finally, quit and reopen the Codex desktop app. Its model list is held in memory
by the background `app-server` process, so a running app keeps showing the old
list until it restarts.

## Expected result

The model picker, top to bottom:

```
GPT-6-Astra, GPT-5.6-Sol, GPT-5.6-Terra, GPT-5.6-Luna, GPT-5.5
Fable 5.1, Opus 5, Sonnet 5, Haiku 4.5                      (Anthropic direct)
Gemini 3.1 Pro, Gemini 3.8 Flash                            (Google Antigravity)
Google Opus 4.6, Google Sonnet 4.6, Google GPT-OSS 120B     (Antigravity third-party)
```

`gpt-5.3-codex-spark` stays hidden.

The `Google *` names mark models served through Antigravity rather than through
the vendor directly. They spend the Antigravity shared pool, not the Anthropic
subscription — which is why they are named apart from `Opus 5` / `Sonnet 5`.

Then `$opencodex-quota-bars` prints live quota gauges for every connected
provider. Providers that are not connected on that machine are simply absent
from the output; that is not an error.

## Updating the profile

After changing model order, visibility, or names on the source machine:

```bash
./export.sh   # rewrites profile.json from the live config
git commit -am "update model profile"
```

To update the skill, copy it back into `skills/` and commit.

## Troubleshooting

**`Missing providers`** — the OAuth login did not create the provider entry.
Verify with `ocx provider list`, and add it from the OpenCodex dashboard
(`ocx gui`) if the login alone did not register it.

**Picker order unchanged after apply** — the desktop app was not restarted.
`ocx sync --restart-codex` restarts only the background app-server, but it
interrupts any in-flight turn.

**A model is missing from the picker** — that model is not available on the new
account. `apply.sh` keeps the rest of the order intact; check
`ocx models live --provider <name>` to see what the account can actually reach.
