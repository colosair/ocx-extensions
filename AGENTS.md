# Instructions for agents

OpenCodex discovers the models and owns the user's hidden models; ocx-extensions curates the
routed models of the policy providers in the Codex picker (newest two generations per family,
newest first) and shows quota. It must work on a new machine from just the repo URL.

Read these rules and `README.md` before acting.

## Agent Bootstrap Flow

When asked to apply this repo, follow this exact sequence:

1. **Verify `ocx` (OpenCodex):** Check if `ocx` is installed (`command -v ocx`).
   - If not installed: use the official OpenCodex upstream as the authoritative
     source for the current installation method:
     https://github.com/lidge-jun/opencodex
     Follow the upstream README to install, then verify with `ocx --version`.
   - If already installed: use the existing installation. Do not reinstall or change its version.
2. **Verify Python:** Ensure `python3` (or `python` 3.x) is available and actually runs
   (on Windows the Microsoft Store `python3` alias may be a stub; use `python` then).
3. **Verify Providers:** The required providers are the `providers[].name` entries in
   `policy.json` (`anthropic`, `google-antigravity`). Check with `ocx provider list`.
   - If either is missing, **stop and ask the user to log in**. Do not try to bypass OAuth or create providers by hand-editing config files. Provide the exact commands for the user to run (e.g., `ocx login anthropic`, `ocx login google-antigravity`). Tell the user to let you know when they are done.
4. **Verify the proxy service:** Run `ocx status`. If it reports an installed background
   service, use it as is. If not, run `ocx service` (installs when absent, repairs otherwise;
   check `ocx service --help` for the current upstream form). Do not reinstall a healthy
   service, and do not recreate OpenCodex's service yourself. If the proxy is not running
   afterwards, ask the user to run `ocx start`.
5. **Apply Configuration:** Execute `./apply.sh` in the repository root.
   - If it reports that the proxy is not running, ask the user to run `ocx start`.
   - **Only on Windows, and only if it stops with exit code 3 (`CodexUserIdentityRefusal`,
     "junction or reparse point", or "unsafe coordinator namespace"):** do not retry it inside the sandbox and do not
     try to get around the check. Run `python scripts/windows-host-apply.py` once; it runs
     apply in the normal Windows context through a temporary task and always deletes that task.
     Then confirm `schtasks /Query /TN ocx-extensions-setup` finds nothing.
6. **Verify and Wrap up:** Ensure apply succeeds (its last step is `reconcile-models.py --check`).
   If it printed the scheduler WARNING, report it; setup still counts as applied. Tell the user
   to quit and reopen the Codex desktop app so the new model picker loads.

## Ownership rules

OpenCodex (and the user through it) owns:
- the model inventory (live discovery) and native model ordering;
- the user's hidden models: `disabledModels`;
- the proxy background service (`ocx service`, Windows task `opencodex-proxy`);
- provider, default-model, and subagent settings not listed in `policy.json`;
- `selectedModels` of every provider not listed in `policy.json` (e.g. `opencode-go`).

ocx-extensions owns:
- managed family classification (rules in `policy.json`, pure functions in `scripts/reconcile-models.py`);
- `providers.<name>.selectedModels` for the providers in `policy.json` (generated: the newest
  `maxGenerationsPerFamily` generations per family, newest first);
- `providers.<name>.modelDisplayNames` for those providers (generated, covers the generated selection);
- the routed `modelPickerOrder`;
- explicit hide exceptions (`ensureDisabled`, append-only);
- the single 15-minute reconcile scheduler entry (Windows task `ocx-extensions-reconcile`);
- the Windows host-native setup fallback (`scripts/windows-host-apply.py`);
- the quota skill.

Rules:

- Never store discovered inventory or generated model lists in git. Do not turn live ids,
  generated `selectedModels`, `modelPickerOrder`, or `modelDisplayNames` back into
  repository state. There is no export step, on purpose.
- **Do not store static `selectedModels` in git.** For the providers in `policy.json`,
  `selectedModels` is generated from the live catalog on every run; a hand-maintained
  allowlist would hide every future model.
- Keep at most `maxGenerationsPerFamily` (2) generations per family, newest first. Do not
  hardcode the number elsewhere; change it only in `policy.json`.
- **Never use `disabledModels` to implement automatic generation pruning.** Old generations
  leave the generated `selectedModels`; they are not disabled.
- Exact model ids in `policy.json` are allowed only for intentional operator
  exceptions such as `ensureDisabled`. Do not pin `subagentModels` or
  `providers.*.defaultModel` there.
- Never overwrite `disabledModels` during ordinary reconcile. Only `--bootstrap` may
  append to it (legacy static allowlist migration and `ensureDisabled`). Never enable a
  model the user disabled.
- Never touch `selectedModels` of providers outside `policy.json`.
- Never preserve stale generated values merely because they already exist; selection, names,
  and order are recomputed every run.
- Never add bare native ids to `modelPickerOrder`.
- New models and new families are picked up automatically. Do not add approval steps,
  quarantine, or manual allowlists; the only filter is the newest-N rule per family.
- Register the scheduler only through `scripts/install-auto-reconcile.py` (run by
  `apply.sh`); it replaces its single entry. Do not add a second task, agent, daemon,
  watcher, service, or cron line, and never make the scheduled run restart the proxy or
  Codex. On Windows it must run `pythonw.exe`, and every `ocx` call must go through
  `build_ocx_command` / `run_hidden` so no console window opens.
- The temporary `ocx-extensions-setup` task exists only while
  `scripts/windows-host-apply.py` runs. Never leave it behind or make it persistent.
- To change naming, family grouping, or ordering, edit the rules in `policy.json` or the
  pure functions in `scripts/reconcile-models.py`, and extend `scripts/test_reconcile_models.py`.

## General Rules

- Do not hand-edit `~/.codex/opencodex-catalog.json` or `~/.codex/models_cache.json`.
  They are regenerated by `ocx sync`.
- Do not copy accounts, tokens, or `~/.opencodex/config.json` wholesale between
  machines. `ocx config export` includes account state and is not portable.
- Do not run `ocx sync --restart-codex` mid-conversation; it kills the active turn.
  Tell the user to quit and reopen the app instead.
- If a provider login is missing, stop and ask the user to log in. Do not invent
  provider entries by editing config directly.
- Do not hardcode specific clone paths. The scripts are path-independent and will work wherever the user cloned it.
- Do not pin an OpenCodex version in this repo; README lists the capabilities it relies on.
