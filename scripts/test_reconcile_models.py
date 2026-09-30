"""Regression tests for the picker reconciler. Run: python3 -B -m unittest discover -s scripts"""

import copy
import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rm = load("reconcile_models", "reconcile-models.py")
sched = load("install_auto_reconcile", "install-auto-reconcile.py")

POLICY = rm.json.loads(rm.POLICY_PATH.read_text(encoding="utf-8"))
RULES = {r["name"]: r for r in POLICY["providers"]}
NATIVE = [{"id": m, "provider": "openai", "namespaced": m, "native": True} for m in ("gpt-5.5", "gpt-6-sol")]


def rows(provider, *ids, source="fallback"):
    return [{"id": i, "provider": provider, "namespaced": f"{provider}/{i}",
             "displayNameSource": source, "displayName": f"{provider}/{i}"} for i in ids]


class FakeOcx:
    def __init__(self, cfg, lives, fail_live=False):
        self.cfg, self.lives, self.fail_live = cfg, lives, fail_live
        self.writes = []

    def config(self):
        return copy.deepcopy(self.cfg)

    def live_all(self):
        if self.fail_live:
            raise rm.OcxError("Proxy is not running")
        return copy.deepcopy(NATIVE + [r for rs in self.lives.values() for r in rs])

    def set(self, path, value):
        self.writes.append(("set", path))
        *parents, leaf = path.split(".")
        node = self.cfg
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = copy.deepcopy(value)

    def unset(self, path):
        self.writes.append(("unset", path))
        *parents, leaf = path.split(".")
        node = self.cfg
        for key in parents:
            node = node[key]
        del node[leaf]

    def sync(self):
        self.writes.append(("sync",))
        return "synced"


def run(ocx, *argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rm.main(list(argv), ocx=ocx, policy=POLICY)
    return code, err.getvalue()


def base_config():
    return {"providers": {"anthropic": {}, "google-antigravity": {}}, "disabledModels": [],
            "modelPickerOrder": ["opencode-go/glm-5.3"]}


CURRENT = {
    "anthropic": rows("anthropic", "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5"),
    "google-antigravity": rows("google-antigravity", "gemini-3.8-flash",
                               "claude-opus-4-6-thinking", "claude-sonnet-4-6"),
}
MANAGED_ORDER = [
    "anthropic/claude-fable-5-1", "anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5",
    "google-antigravity/gemini-3.8-flash", "google-antigravity/claude-opus-4-6-thinking",
    "google-antigravity/claude-sonnet-4-6",
]


class NamingTest(unittest.TestCase):
    def name(self, provider, model_id):
        return rm.display_name_for(rows(provider, model_id)[0], RULES[provider])

    def test_current_models_keep_their_picker_names(self):
        expected = {
            ("anthropic", "claude-fable-5-1"): "Fable 5.1",
            ("anthropic", "claude-opus-5-5"): "Opus 5.5",
            ("anthropic", "claude-sonnet-5"): "Sonnet 5",
            ("anthropic", "claude-haiku-4-5-20251001"): "Haiku 4.5",
            ("google-antigravity", "gemini-3.8-flash"): "Gemini 3.8 Flash",
            ("google-antigravity", "gemini-3.1-pro"): "Gemini 3.1 Pro",
            ("google-antigravity", "claude-opus-4-6-thinking"): "Google Opus 4.6",
            ("google-antigravity", "claude-sonnet-4-6"): "Google Sonnet 4.6",
            ("google-antigravity", "gpt-oss-120b-medium"): "Google GPT-OSS 120B",
        }
        for (provider, model_id), name in expected.items():
            self.assertEqual(self.name(provider, model_id), name, model_id)

    def test_new_generations_are_named_by_rule(self):
        self.assertEqual(self.name("anthropic", "claude-opus-5-6"), "Opus 5.6")
        self.assertEqual(self.name("anthropic", "claude-sonnet-5-1"), "Sonnet 5.1")
        self.assertEqual(self.name("google-antigravity", "gemini-3.9-flash"), "Gemini 3.9 Flash")
        self.assertEqual(self.name("google-antigravity", "claude-opus-5-0-thinking"), "Google Opus 5.0")
        self.assertEqual(self.name("google-antigravity", "gemini-4-pro-preview"), "Gemini 4 Pro Preview")

    def test_unknown_family_prefers_provider_name_then_humanizer(self):
        nova = rows("anthropic", "claude-nova-6")[0]
        self.assertEqual(rm.display_name_for(nova, RULES["anthropic"]), "Nova 6")
        nova.update(displayNameSource="provider", displayName="Claude Nova 6")
        self.assertIsNone(rm.display_name_for(nova, RULES["anthropic"]))  # OpenCodex shows provider name
        odd = rows("anthropic", "Claude_Odd")[0]
        self.assertIsNone(rm.display_name_for(odd, RULES["anthropic"]))  # raw id stays

    def test_colliding_names_keep_the_stripped_qualifier(self):
        names = rm.build_display_names(
            rows("google-antigravity", "claude-opus-4-6", "claude-opus-4-6-thinking"),
            RULES["google-antigravity"])
        self.assertEqual(names, {"claude-opus-4-6": "Google Opus 4.6",
                                 "claude-opus-4-6-thinking": "Google Opus 4.6 Thinking"})


class ReconcileTest(unittest.TestCase):
    def test_current_catalog_keeps_order_and_names(self):
        ocx = FakeOcx(base_config(), CURRENT)
        self.assertEqual(run(ocx)[0], 0)
        self.assertEqual(ocx.cfg["modelPickerOrder"], MANAGED_ORDER + ["opencode-go/glm-5.3"])
        self.assertEqual(ocx.cfg["providers"]["anthropic"]["modelDisplayNames"],
                         {"claude-fable-5-1": "Fable 5.1", "claude-opus-5-5": "Opus 5.5", "claude-sonnet-5": "Sonnet 5"})

    def test_new_same_family_model_appears_next_to_old_one(self):
        lives = copy.deepcopy(CURRENT)
        lives["anthropic"] += rows("anthropic", "claude-opus-5-6")
        ocx = FakeOcx(base_config(), lives)
        run(ocx)
        order = ocx.cfg["modelPickerOrder"]
        self.assertEqual(order.index("anthropic/claude-opus-5-6"), order.index("anthropic/claude-opus-5-5") + 1)
        names = ocx.cfg["providers"]["anthropic"]["modelDisplayNames"]
        self.assertEqual((names["claude-opus-5-5"], names["claude-opus-5-6"]), ("Opus 5.5", "Opus 5.6"))
        self.assertEqual(ocx.cfg["disabledModels"], [])

    def test_new_gemini_and_unknown_family_are_listed(self):
        lives = copy.deepcopy(CURRENT)
        lives["google-antigravity"] += rows("google-antigravity", "gemini-3.9-flash")
        lives["anthropic"] += rows("anthropic", "claude-nova-6")
        ocx = FakeOcx(base_config(), lives)
        run(ocx)
        order = ocx.cfg["modelPickerOrder"]
        self.assertLess(order.index("anthropic/claude-sonnet-5"), order.index("anthropic/claude-nova-6"))
        self.assertEqual(order.index("google-antigravity/gemini-3.9-flash"),
                         order.index("google-antigravity/gemini-3.8-flash") + 1)
        self.assertEqual(ocx.cfg["providers"]["anthropic"]["modelDisplayNames"]["claude-nova-6"], "Nova 6")
        self.assertEqual(ocx.cfg["providers"]["google-antigravity"]["modelDisplayNames"]["gemini-3.9-flash"],
                         "Gemini 3.9 Flash")

    def test_stale_generated_name_is_corrected(self):  # A
        cfg = base_config()
        cfg["providers"]["anthropic"]["modelDisplayNames"] = {"claude-opus-5-5": "Opus 5 5"}
        ocx = FakeOcx(cfg, CURRENT)
        run(ocx)
        self.assertEqual(ocx.cfg["providers"]["anthropic"]["modelDisplayNames"]["claude-opus-5-5"], "Opus 5.5")

    def test_provider_named_unknown_family_gets_no_override(self):  # B
        lives = copy.deepcopy(CURRENT)
        lives["anthropic"] += rows("anthropic", "claude-nova-6", source="provider")
        ocx = FakeOcx(base_config(), lives)
        run(ocx)
        self.assertNotIn("claude-nova-6", ocx.cfg["providers"]["anthropic"]["modelDisplayNames"])
        self.assertIn("anthropic/claude-nova-6", ocx.cfg["modelPickerOrder"])

    def test_stale_name_keys_are_removed_and_empty_map_unset(self):  # C
        cfg = base_config()
        cfg["providers"]["anthropic"]["modelDisplayNames"] = {"claude-opus-4-1": "Opus 4.1", "claude-opus-5-5": "Opus 5.5"}
        lives = copy.deepcopy(CURRENT)
        lives["anthropic"] += rows("anthropic", "claude-nova-6", source="provider")
        ocx = FakeOcx(cfg, lives)
        run(ocx)
        self.assertNotIn("claude-opus-4-1", ocx.cfg["providers"]["anthropic"]["modelDisplayNames"])

        cfg = base_config()
        cfg["providers"]["anthropic"]["modelDisplayNames"] = {"claude-gone-1": "Gone 1"}
        ocx = FakeOcx(cfg, {**CURRENT, "anthropic": rows("anthropic", "claude-nova-6", source="provider")})
        run(ocx)
        self.assertNotIn("modelDisplayNames", ocx.cfg["providers"]["anthropic"])
        self.assertIn(("unset", "providers.anthropic.modelDisplayNames"), ocx.writes)

    def test_new_unmanaged_routed_model_joins_the_order(self):  # D
        cfg = base_config()
        cfg["modelPickerOrder"] = MANAGED_ORDER + ["opencode-go/model-z", "opencode-go/model-a"]
        lives = {**copy.deepcopy(CURRENT),
                 "opencode-go": rows("opencode-go", "model-a", "model-b", "model-z"),
                 "zai": rows("zai", "glm-9")}
        ocx = FakeOcx(cfg, lives)
        run(ocx)
        self.assertEqual(ocx.cfg["modelPickerOrder"], MANAGED_ORDER + [
            "opencode-go/model-z", "opencode-go/model-a", "opencode-go/model-b", "zai/glm-9"])

    def test_bare_native_ids_never_enter_the_order(self):  # E
        cfg = base_config()
        cfg["modelPickerOrder"] = ["gpt-5.5"] + MANAGED_ORDER
        ocx = FakeOcx(cfg, CURRENT)
        run(ocx)
        self.assertTrue(all("/" in slug for slug in ocx.cfg["modelPickerOrder"]))

    def test_ordinary_runs_never_write_disabled_models(self):  # F
        cfg = base_config()
        cfg["disabledModels"] = ["anthropic/claude-haiku-5", "google-antigravity/gemini-old"]
        cfg["providers"]["anthropic"]["modelDisplayNames"] = {"claude-opus-5-5": "Opus 5 5"}
        lives = copy.deepcopy(CURRENT)
        lives["anthropic"] += rows("anthropic", "claude-haiku-5", "claude-opus-5-6")
        ocx = FakeOcx(cfg, lives)
        for _ in range(3):
            self.assertEqual(run(ocx)[0], 0)
        self.assertEqual(ocx.cfg["disabledModels"], ["anthropic/claude-haiku-5", "google-antigravity/gemini-old"])
        self.assertNotIn(("set", "disabledModels"), ocx.writes)
        self.assertIn("anthropic/claude-haiku-5", ocx.cfg["modelPickerOrder"])  # keeps its slot for re-enable

    def test_second_run_changes_nothing(self):
        lives = {**copy.deepcopy(CURRENT), "opencode-go": rows("opencode-go", "model-a")}
        ocx = FakeOcx(base_config(), lives)
        run(ocx, "--bootstrap")
        ocx.writes.clear()
        self.assertEqual(run(ocx)[0], 0)
        self.assertEqual(ocx.writes, [])
        self.assertEqual(run(ocx, "--check")[0], 0)

    def test_failed_discovery_writes_nothing(self):
        ocx = FakeOcx(base_config(), CURRENT, fail_live=True)
        code, err = run(ocx, "--bootstrap")
        self.assertEqual(code, 1)
        self.assertIn("config untouched", err)
        self.assertEqual(ocx.writes, [])
        ocx = FakeOcx(base_config(), {**CURRENT, "anthropic": []})
        self.assertEqual(run(ocx)[0], 1)
        self.assertEqual(ocx.writes, [])

    def test_missing_provider_stops_bootstrap_before_any_write(self):
        cfg = base_config()
        del cfg["providers"]["google-antigravity"]
        ocx = FakeOcx(cfg, CURRENT)
        code, err = run(ocx, "--bootstrap")
        self.assertEqual(code, 1)
        self.assertIn("ocx login google-antigravity", err)
        self.assertEqual(ocx.writes, [])
        self.assertNotIn("google-antigravity", ocx.cfg["providers"])


class MigrationTest(unittest.TestCase):
    def test_selected_models_become_disabled_models_once(self):
        cfg = base_config()
        cfg["disabledModels"] = ["anthropic/claude-haiku-4-5-20251001"]
        cfg["providers"]["anthropic"]["selectedModels"] = ["claude-opus-5-5", "claude-haiku-4-5-20251001"]
        cfg["modelDiscovery"] = {"knownModels": {"anthropic": {"ids": [
            "claude-opus-4-8", "claude-opus-5-5", "claude-haiku-4-5-20251001"]}}}
        lives = copy.deepcopy(CURRENT)
        lives["anthropic"] = rows("anthropic", "claude-opus-4-8", "claude-opus-5-5",
                                  "claude-haiku-4-5-20251001", "claude-sonnet-5-5")
        ocx = FakeOcx(cfg, lives)
        self.assertEqual(run(ocx, "--bootstrap")[0], 0)
        self.assertNotIn("selectedModels", ocx.cfg["providers"]["anthropic"])
        # hidden-and-known -> disabled; user entry kept; unseen arrival stays visible
        self.assertEqual(ocx.cfg["disabledModels"], [
            "anthropic/claude-haiku-4-5-20251001", "gpt-5.3-codex-spark", "anthropic/claude-opus-4-8"])
        self.assertEqual(ocx.cfg["modelDiscovery"]["newModelPolicy"], "on")
        self.assertEqual(ocx.cfg["catalogAutoRefresh"], {"enabled": True, "intervalMinutes": 15})


class PolicyTest(unittest.TestCase):
    def test_policy_does_not_pin_models_or_generated_state(self):  # G
        for path in POLICY["config"]:
            for owned_elsewhere in ("subagentModels", "defaultModel", "selectedModels", "disabledModels",
                                    "modelDisplayNames", "modelPickerOrder"):
                self.assertNotIn(owned_elsewhere, path)


class SchedulerTest(unittest.TestCase):  # H
    def test_windows_task_is_one_named_task_overwritten_in_place(self):
        cmd = sched.windows_command(r"C:\Py\pythonw.exe", r"D:\repo\scripts\reconcile-models.py")
        self.assertEqual(cmd, sched.windows_command(r"C:\Py\pythonw.exe", r"D:\repo\scripts\reconcile-models.py"))
        self.assertEqual(cmd[cmd.index("/TN") + 1], sched.NAME)
        self.assertIn("/F", cmd)
        self.assertEqual(cmd[cmd.index("/MO") + 1], "15")

    def test_cron_entry_is_replaced_not_duplicated(self):
        first = sched.cron_table("MAILTO=me\n0 1 * * * backup\n", "/usr/bin/python3", "/old/reconcile-models.py", "/bin", "/tmp/l")
        again = sched.cron_table(first, "/usr/bin/python3", "/old/reconcile-models.py", "/bin", "/tmp/l")
        moved = sched.cron_table(again, "/opt/py3", "/new path/reconcile-models.py", "/bin", "/tmp/l")
        self.assertEqual(first, again)
        self.assertEqual(moved.count(sched.NAME), 1)
        self.assertIn("'/new path/reconcile-models.py'", moved)
        self.assertTrue(moved.startswith("MAILTO=me\n0 1 * * * backup\n*/15 "))

    def test_launchd_plist_is_deterministic(self):
        plist = sched.launchd_plist("/usr/bin/python3", "/r/reconcile-models.py", "/bin", "/tmp/l")
        self.assertEqual(plist, sched.launchd_plist("/usr/bin/python3", "/r/reconcile-models.py", "/bin", "/tmp/l"))
        data = sched.plistlib.loads(plist)
        self.assertEqual((data["Label"], data["StartInterval"]), (sched.LABEL, 900))


if __name__ == "__main__":
    unittest.main()
