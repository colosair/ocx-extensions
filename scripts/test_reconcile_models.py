"""Regression tests for the picker reconciler. Run: python3 -B -m unittest discover -s scripts"""

import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HERE = Path(__file__).resolve().parent
rm = load("reconcile_models", HERE / "reconcile-models.py")
sched = load("install_auto_reconcile", HERE / "install-auto-reconcile.py")
host = load("windows_host_apply", HERE / "windows-host-apply.py")
quota = load("quota_bars_for_reconcile_tests", HERE.parent / "skills" / "ocx-quota" / "scripts" / "quota-bars.py")

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


def run(ocx, *argv, policy=POLICY):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rm.main(list(argv), ocx=ocx, policy=policy)
    return code, err.getvalue()


def base_config():
    return {"providers": {"anthropic": {}, "google-antigravity": {}}, "disabledModels": [],
            "modelDiscovery": {"newModelPolicy": "on"}, "modelPickerOrder": ["opencode-go/glm-5.3"]}


def selected(ocx, provider="anthropic"):
    return ocx.cfg["providers"][provider]["selectedModels"]


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
OPUS_HISTORY = ("claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
                "claude-opus-5", "claude-opus-5-5")


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
            ("google-antigravity", "gemini-3.1-flash-image"): "Gemini 3.1 Flash Image",
            ("google-antigravity", "claude-opus-4-6-thinking"): "Google Opus 4.6",
            ("google-antigravity", "claude-opus-5-5-high"): "Google Opus 5.5",
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


class GenerationTest(unittest.TestCase):
    def test_family_generation_and_variant_are_separate(self):
        cases = {
            "claude-opus-4-6": ("claude-opus", (4, 6), ()),
            "claude-opus-4-6-thinking": ("claude-opus", (4, 6), ("thinking",)),
            "claude-haiku-4-5-20251001": ("claude-haiku", (4, 5), ("20251001",)),
            "claude-opus-5": ("claude-opus", (5,), ()),
            "claude-opus-5-0-thinking": ("claude-opus", (5,), ("thinking",)),
            "claude-3-5-sonnet-20241022": ("claude-sonnet", (3, 5), ("20241022",)),
            "gemini-3.8-flash": ("gemini-flash", (3, 8), ()),
            "gemini-3.1-pro": ("gemini-pro", (3, 1), ()),
            "gemini-3.1-flash-image": ("gemini-flash-image", (3, 1), ()),
            "gemini-3.5-flash-extra-low": ("gemini-flash", (3, 5), ("extra", "low")),
            "gemini-4-pro-preview": ("gemini-pro", (4,), ("preview",)),
            "gpt-oss-120b-medium": ("gpt-oss-120b", (), ("medium",)),
        }
        for model_id, parts in cases.items():
            self.assertEqual(rm.model_parts(model_id), parts, model_id)
            self.assertEqual((rm.family_for(model_id), rm.generation_for(model_id), rm.variant_for(model_id)), parts)

    def test_preferred_variant_order(self):
        self.assertEqual(rm.preferred_variant(["claude-haiku-4-5-20251001", "claude-haiku-4-5"]), "claude-haiku-4-5")
        self.assertEqual(rm.preferred_variant(["gemini-4-pro-preview", "gemini-4-pro"]), "gemini-4-pro")
        self.assertEqual(rm.preferred_variant(["claude-opus-4-6-thinking", "claude-opus-4-6-20260101"]),
                         "claude-opus-4-6-20260101")
        self.assertEqual(rm.preferred_variant(["claude-opus-5-5-low", "claude-opus-5-5-high", "claude-opus-5-5-medium"]),
                         "claude-opus-5-5-high")
        # Only a capability variant exists: it is used rather than losing the generation.
        self.assertEqual(rm.preferred_variant(["claude-opus-4-6-thinking"]), "claude-opus-4-6-thinking")

    def test_family_rank_prefers_the_longest_policy_entry(self):
        rules = RULES["google-antigravity"]
        families = rules["families"]
        self.assertEqual(rm.family_rank("gemini-flash-image", rules), families.index("gemini-flash-image"))
        self.assertEqual(rm.family_rank("gemini-flash-lite", rules), families.index("gemini-flash"))
        self.assertEqual(rm.family_rank("gpt-oss-120b", rules), families.index("gpt-oss"))
        self.assertEqual(rm.family_rank("claude-nova", rules), len(families))


class LatestGenerationsTest(unittest.TestCase):
    def test_only_the_newest_two_generations_are_selected(self):  # 29
        ocx = FakeOcx(base_config(), {**CURRENT, "anthropic": rows("anthropic", *OPUS_HISTORY)})
        self.assertEqual(run(ocx)[0], 0)
        self.assertEqual(selected(ocx), ["claude-opus-5-5", "claude-opus-5"])
        self.assertEqual(ocx.cfg["providers"]["anthropic"]["modelDisplayNames"],
                         {"claude-opus-5-5": "Opus 5.5", "claude-opus-5": "Opus 5"})
        self.assertEqual(ocx.cfg["modelPickerOrder"][:2], ["anthropic/claude-opus-5-5", "anthropic/claude-opus-5"])
        for old in OPUS_HISTORY[:4]:
            self.assertNotIn(f"anthropic/{old}", ocx.cfg["modelPickerOrder"])

    def test_newest_generation_is_listed_first(self):  # 30
        ocx = FakeOcx(base_config(), {**CURRENT, "anthropic": rows("anthropic", "claude-opus-5", "claude-opus-5-5")})
        run(ocx)
        self.assertNotEqual(selected(ocx), ["claude-opus-5", "claude-opus-5-5"])
        self.assertEqual(selected(ocx), ["claude-opus-5-5", "claude-opus-5"])
        order = ocx.cfg["modelPickerOrder"]
        self.assertLess(order.index("anthropic/claude-opus-5-5"), order.index("anthropic/claude-opus-5"))

    def test_every_family_is_newest_first_in_policy_order(self):
        lives = {"anthropic": rows("anthropic", "claude-haiku-4-5", "claude-sonnet-5", "claude-sonnet-5-5",
                                   "claude-opus-5", "claude-opus-5-5", "claude-fable-5", "claude-fable-5-1")}
        lives["google-antigravity"] = CURRENT["google-antigravity"]
        ocx = FakeOcx(base_config(), lives)
        run(ocx)
        self.assertEqual(selected(ocx), ["claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5",
                                         "claude-sonnet-5-5", "claude-sonnet-5", "claude-haiku-4-5"])

    def test_a_new_generation_slides_the_window(self):  # 31
        ocx = FakeOcx(base_config(), {**CURRENT, "anthropic": rows("anthropic", "claude-opus-5", "claude-opus-5-5")})
        run(ocx)
        self.assertEqual(selected(ocx), ["claude-opus-5-5", "claude-opus-5"])
        ocx.lives["anthropic"] += rows("anthropic", "claude-opus-6")
        self.assertEqual(run(ocx)[0], 0)
        self.assertEqual(selected(ocx), ["claude-opus-6", "claude-opus-5-5"])
        self.assertEqual(ocx.cfg["providers"]["anthropic"]["modelDisplayNames"],
                         {"claude-opus-6": "Opus 6", "claude-opus-5-5": "Opus 5.5"})
        self.assertEqual(ocx.cfg["disabledModels"], [])  # Opus 5 leaves the picker; it is not disabled

    def test_variants_of_one_generation_count_once(self):  # 32
        ids = ("claude-opus-4-6", "claude-opus-4-6-thinking", "claude-opus-5", "claude-opus-5-5")
        self.assertEqual(rm.select_models(ids, RULES["anthropic"], 2), ["claude-opus-5-5", "claude-opus-5"])
        self.assertEqual(rm.select_models(ids, RULES["anthropic"], 3),
                         ["claude-opus-5-5", "claude-opus-5", "claude-opus-4-6"])
        ids = ("claude-opus-5-5-thinking", "claude-opus-5-5", "claude-opus-5")
        self.assertEqual(rm.select_models(ids, RULES["anthropic"], 2), ["claude-opus-5-5", "claude-opus-5"])

    def test_dated_snapshot_collapses_into_its_generation(self):  # 33
        ids = ("claude-haiku-4-5", "claude-haiku-4-5-20251001")
        self.assertEqual(rm.select_models(ids, RULES["anthropic"], 2), ["claude-haiku-4-5"])
        self.assertEqual(rm.select_models(ids[1:], RULES["anthropic"], 2), ["claude-haiku-4-5-20251001"])

    def test_user_disabled_model_stays_disabled_and_keeps_its_name(self):  # 34
        cfg = base_config()
        cfg["disabledModels"] = ["anthropic/claude-opus-5"]
        ocx = FakeOcx(cfg, {**CURRENT, "anthropic": rows("anthropic", "claude-opus-5", "claude-opus-5-5")})
        for _ in range(3):
            self.assertEqual(run(ocx)[0], 0)
        self.assertEqual(ocx.cfg["disabledModels"], ["anthropic/claude-opus-5"])
        self.assertNotIn(("set", "disabledModels"), ocx.writes)
        # Still selected (OpenCodex hides it via disabledModels), so re-enabling shows "Opus 5".
        self.assertEqual(selected(ocx), ["claude-opus-5-5", "claude-opus-5"])
        self.assertEqual(ocx.cfg["providers"]["anthropic"]["modelDisplayNames"]["claude-opus-5"], "Opus 5")

    def test_unmanaged_provider_selection_is_untouched(self):  # 35
        cfg = base_config()
        cfg["providers"]["opencode-go"] = {"selectedModels": [f"model-{i}" for i in range(7)], "alias": "go"}
        before = json.dumps(cfg["providers"]["opencode-go"], sort_keys=True)
        lives = {**CURRENT, "opencode-go": rows("opencode-go", *[f"model-{i}" for i in range(12)])}
        ocx = FakeOcx(cfg, lives)
        self.assertEqual(run(ocx, "--bootstrap")[0], 0)
        for _ in range(2):
            self.assertEqual(run(ocx)[0], 0)
        self.assertEqual(json.dumps(ocx.cfg["providers"]["opencode-go"], sort_keys=True), before)
        self.assertFalse([w for w in ocx.writes if "opencode-go" in w[-1]])

    def test_unknown_family_is_kept_and_trimmed_like_any_family(self):  # 36
        lives = copy.deepcopy(CURRENT)
        lives["anthropic"] += rows("anthropic", "claude-nova-6", "claude-nova-7", "claude-nova-8", source="provider")
        lives["anthropic"] += rows("anthropic", "claude-zeta-1")
        ocx = FakeOcx(base_config(), lives)
        run(ocx)
        self.assertEqual(selected(ocx), ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5",
                                         "claude-nova-8", "claude-nova-7", "claude-zeta-1"])
        names = ocx.cfg["providers"]["anthropic"]["modelDisplayNames"]
        self.assertNotIn("claude-nova-8", names)  # provider-authored name wins
        self.assertEqual(names["claude-zeta-1"], "Zeta 1")

    def test_gemini_flash_and_pro_keep_separate_windows(self):  # 37
        lives = copy.deepcopy(CURRENT)
        lives["google-antigravity"] = rows("google-antigravity", "gemini-3.6-flash", "gemini-3.7-flash",
                                           "gemini-3.8-flash", "gemini-3.9-flash", "gemini-3.1-pro",
                                           "gemini-3.1-flash-image")
        ocx = FakeOcx(base_config(), lives)
        run(ocx)
        self.assertEqual(selected(ocx, "google-antigravity"),
                         ["gemini-3.9-flash", "gemini-3.8-flash", "gemini-3.1-pro", "gemini-3.1-flash-image"])

    def test_generation_limit_comes_from_policy(self):
        policy = {**POLICY, "maxGenerationsPerFamily": 3}
        ocx = FakeOcx(base_config(), {**CURRENT, "anthropic": rows("anthropic", *OPUS_HISTORY)})
        run(ocx, policy=policy)
        self.assertEqual(selected(ocx), ["claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"])
        with self.assertRaises(ValueError):
            rm.generation_limit({**POLICY, "maxGenerationsPerFamily": 0})

    def test_check_covers_selected_models(self):  # 14
        ocx = FakeOcx(base_config(), CURRENT)
        run(ocx)
        self.assertEqual(run(ocx, "--check")[0], 0)
        ocx.cfg["providers"]["anthropic"]["selectedModels"] = ["claude-opus-5-5"]
        code, err = run(ocx, "--check")
        self.assertEqual(code, 1)
        self.assertIn("providers.anthropic.selectedModels", err)
        self.assertNotIn("disabledModels", err)


class ReconcileTest(unittest.TestCase):
    def test_current_catalog_keeps_order_and_names(self):
        ocx = FakeOcx(base_config(), CURRENT)
        self.assertEqual(run(ocx)[0], 0)
        self.assertEqual(ocx.cfg["modelPickerOrder"], MANAGED_ORDER + ["opencode-go/glm-5.3"])
        self.assertEqual(ocx.cfg["providers"]["anthropic"]["modelDisplayNames"],
                         {"claude-fable-5-1": "Fable 5.1", "claude-opus-5-5": "Opus 5.5", "claude-sonnet-5": "Sonnet 5"})

    def test_new_same_family_model_appears_above_the_old_one(self):
        lives = copy.deepcopy(CURRENT)
        lives["anthropic"] += rows("anthropic", "claude-opus-5-6")
        ocx = FakeOcx(base_config(), lives)
        run(ocx)
        order = ocx.cfg["modelPickerOrder"]
        self.assertEqual(order.index("anthropic/claude-opus-5-6") + 1, order.index("anthropic/claude-opus-5-5"))
        names = ocx.cfg["providers"]["anthropic"]["modelDisplayNames"]
        self.assertEqual((names["claude-opus-5-6"], names["claude-opus-5-5"]), ("Opus 5.6", "Opus 5.5"))
        self.assertEqual(ocx.cfg["disabledModels"], [])

    def test_new_gemini_and_unknown_family_are_listed(self):
        lives = copy.deepcopy(CURRENT)
        lives["google-antigravity"] += rows("google-antigravity", "gemini-3.9-flash")
        lives["anthropic"] += rows("anthropic", "claude-nova-6")
        ocx = FakeOcx(base_config(), lives)
        run(ocx)
        order = ocx.cfg["modelPickerOrder"]
        self.assertLess(order.index("anthropic/claude-sonnet-5"), order.index("anthropic/claude-nova-6"))
        self.assertEqual(order.index("google-antigravity/gemini-3.9-flash") + 1,
                         order.index("google-antigravity/gemini-3.8-flash"))
        self.assertEqual(ocx.cfg["providers"]["anthropic"]["modelDisplayNames"]["claude-nova-6"], "Nova 6")
        self.assertEqual(ocx.cfg["providers"]["google-antigravity"]["modelDisplayNames"]["gemini-3.9-flash"],
                         "Gemini 3.9 Flash")

    def test_stale_generated_name_is_corrected(self):
        cfg = base_config()
        cfg["providers"]["anthropic"]["modelDisplayNames"] = {"claude-opus-5-5": "Opus 5 5"}
        ocx = FakeOcx(cfg, CURRENT)
        run(ocx)
        self.assertEqual(ocx.cfg["providers"]["anthropic"]["modelDisplayNames"]["claude-opus-5-5"], "Opus 5.5")

    def test_provider_named_unknown_family_gets_no_override(self):
        lives = copy.deepcopy(CURRENT)
        lives["anthropic"] += rows("anthropic", "claude-nova-6", source="provider")
        ocx = FakeOcx(base_config(), lives)
        run(ocx)
        self.assertNotIn("claude-nova-6", ocx.cfg["providers"]["anthropic"]["modelDisplayNames"])
        self.assertIn("anthropic/claude-nova-6", ocx.cfg["modelPickerOrder"])

    def test_names_cover_only_the_selection_and_empty_map_is_unset(self):
        cfg = base_config()
        cfg["providers"]["anthropic"]["modelDisplayNames"] = {"claude-opus-4-1": "Opus 4.1", "claude-opus-5-5": "Opus 5.5"}
        lives = copy.deepcopy(CURRENT)
        lives["anthropic"] += rows("anthropic", "claude-opus-4-1", "claude-opus-4-8")
        ocx = FakeOcx(cfg, lives)
        run(ocx)
        names = ocx.cfg["providers"]["anthropic"]["modelDisplayNames"]
        self.assertNotIn("claude-opus-4-1", names)
        self.assertEqual(set(names), set(selected(ocx)))

        cfg = base_config()
        cfg["providers"]["anthropic"]["modelDisplayNames"] = {"claude-gone-1": "Gone 1"}
        ocx = FakeOcx(cfg, {**CURRENT, "anthropic": rows("anthropic", "claude-nova-6", source="provider")})
        run(ocx)
        self.assertNotIn("modelDisplayNames", ocx.cfg["providers"]["anthropic"])
        self.assertIn(("unset", "providers.anthropic.modelDisplayNames"), ocx.writes)

    def test_new_unmanaged_routed_model_joins_the_order(self):
        cfg = base_config()
        cfg["modelPickerOrder"] = MANAGED_ORDER + ["opencode-go/model-z", "opencode-go/model-a"]
        lives = {**copy.deepcopy(CURRENT),
                 "opencode-go": rows("opencode-go", "model-a", "model-b", "model-z"),
                 "zai": rows("zai", "glm-9")}
        ocx = FakeOcx(cfg, lives)
        run(ocx)
        self.assertEqual(ocx.cfg["modelPickerOrder"], MANAGED_ORDER + [
            "opencode-go/model-z", "opencode-go/model-a", "opencode-go/model-b", "zai/glm-9"])

    def test_bare_native_ids_never_enter_the_order(self):
        cfg = base_config()
        cfg["modelPickerOrder"] = ["gpt-5.5"] + MANAGED_ORDER
        ocx = FakeOcx(cfg, CURRENT)
        run(ocx)
        self.assertTrue(all("/" in slug for slug in ocx.cfg["modelPickerOrder"]))

    def test_ordinary_runs_never_write_disabled_models(self):
        cfg = base_config()
        cfg["disabledModels"] = ["anthropic/claude-haiku-5", "google-antigravity/gemini-old"]
        cfg["providers"]["anthropic"]["modelDisplayNames"] = {"claude-opus-5-5": "Opus 5 5"}
        lives = copy.deepcopy(CURRENT)
        lives["anthropic"] += rows("anthropic", "claude-haiku-5", "claude-opus-5-6", "claude-opus-4-1")
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
        self.assertEqual(run(ocx, "--bootstrap")[0], 0)
        self.assertEqual(ocx.writes, [("sync",)])  # a repeated apply only re-syncs

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

    def test_output_survives_a_cp949_stream(self):
        class Warning(FakeOcx):
            def sync(self):
                return "⚠  Codex config updated"
        out, err = io.BytesIO(), io.BytesIO()
        streams = io.TextIOWrapper(out, encoding="cp949"), io.TextIOWrapper(err, encoding="cp949")
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = streams
        try:
            code = rm.main([], ocx=Warning(base_config(), CURRENT), policy=POLICY)
            sys.stdout.flush()
        finally:
            sys.stdout, sys.stderr = saved
        self.assertEqual(code, 0)
        self.assertIn("⚠  Codex config updated", out.getvalue().decode("utf-8"))

    def test_reparse_point_refusal_points_to_the_host_native_helper(self):
        class Refusing(FakeOcx):
            def sync(self):
                raise rm.OcxError("ocx sync: CodexUserIdentityRefusal: The Windows coordinator namespace "
                                  "is redirected by a junction or reparse point..")
        code, err = run(Refusing(base_config(), CURRENT))
        self.assertEqual(code, 3)
        self.assertIn("windows-host-apply.py", err)
        self.assertIsNone(rm.refusal_hint("ocx sync: Proxy is not running"))

    def test_sync_that_silently_skipped_the_catalog_is_reported(self):
        class Skipping(FakeOcx):
            def sync(self):
                return ("  Codex model catalog: C:\\Users\\me\\.codex\\opencodex-catalog.json\n"
                        "  ⚠️ Codex resume history NOT changed: opencodex refused its history lock path "
                        "(unsafe coordinator namespace); this is not a Codex app lock.")
        code, err = run(Skipping(base_config(), CURRENT), "--bootstrap")
        self.assertEqual(code, 3)
        self.assertIn("windows-host-apply.py", err)


class MigrationTest(unittest.TestCase):
    def legacy_config(self):
        cfg = base_config()
        del cfg["modelDiscovery"]["newModelPolicy"]  # the static profile never set it
        cfg["disabledModels"] = ["anthropic/claude-haiku-4-5-20251001"]
        cfg["providers"]["anthropic"]["selectedModels"] = ["claude-opus-5-5", "claude-haiku-4-5-20251001"]
        cfg["modelDiscovery"]["knownModels"] = {"anthropic": {"ids": [
            "claude-opus-4-8", "claude-opus-5-5", "claude-haiku-4-5-20251001"]}}
        lives = copy.deepcopy(CURRENT)
        lives["anthropic"] = rows("anthropic", "claude-opus-4-8", "claude-opus-5-5",
                                  "claude-haiku-4-5-20251001", "claude-sonnet-5-5")
        return cfg, lives

    def test_legacy_allowlist_is_migrated_then_regenerated_once(self):
        cfg, lives = self.legacy_config()
        ocx = FakeOcx(cfg, lives)
        self.assertEqual(run(ocx, "--bootstrap")[0], 0)
        # hidden-and-known -> disabled; user entry kept; unseen arrival stays visible
        self.assertEqual(ocx.cfg["disabledModels"], [
            "anthropic/claude-haiku-4-5-20251001", "gpt-5.3-codex-spark", "anthropic/claude-opus-4-8"])
        self.assertEqual(selected(ocx), ["claude-opus-5-5", "claude-opus-4-8", "claude-sonnet-5-5",
                                         "claude-haiku-4-5-20251001"])
        self.assertEqual(ocx.cfg["modelDiscovery"]["newModelPolicy"], "on")
        self.assertEqual(ocx.cfg["catalogAutoRefresh"], {"enabled": True, "intervalMinutes": 15})
        # A repeated apply.sh sees generated state now: nothing is migrated again.
        migrated = copy.deepcopy(ocx.cfg)
        self.assertEqual(run(ocx, "--bootstrap")[0], 0)
        self.assertEqual(ocx.cfg, migrated)

    def test_stale_generated_list_is_not_mistaken_for_legacy(self):
        cfg = base_config()
        cfg["providers"]["anthropic"]["selectedModels"] = ["claude-opus-5-5", "claude-opus-5"]
        cfg["modelDiscovery"]["knownModels"] = {"anthropic": {"ids": ["claude-opus-5", "claude-opus-5-5", "claude-opus-6"]}}
        lives = {**CURRENT, "anthropic": rows("anthropic", "claude-opus-5", "claude-opus-5-5", "claude-opus-6")}
        ocx = FakeOcx(cfg, lives)
        self.assertEqual(run(ocx, "--bootstrap")[0], 0)
        self.assertEqual(ocx.cfg["disabledModels"], ["gpt-5.3-codex-spark"])  # only ensureDisabled
        self.assertEqual(selected(ocx), ["claude-opus-6", "claude-opus-5-5"])

    def test_scheduled_run_leaves_a_legacy_allowlist_for_apply(self):
        cfg, lives = self.legacy_config()
        ocx = FakeOcx(cfg, lives)
        code, err = run(ocx)
        self.assertEqual(code, 0)
        self.assertIn("legacy static allowlist", err)
        self.assertEqual(selected(ocx), ["claude-opus-5-5", "claude-haiku-4-5-20251001"])
        self.assertNotIn(("set", "disabledModels"), ocx.writes)


class PolicyTest(unittest.TestCase):
    def test_policy_does_not_pin_models_or_generated_state(self):
        for path in POLICY["config"]:
            for owned_elsewhere in ("subagentModels", "defaultModel", "selectedModels", "disabledModels",
                                    "modelDisplayNames", "modelPickerOrder"):
                self.assertNotIn(owned_elsewhere, path)
        self.assertEqual(rm.generation_limit(POLICY), 2)


class HiddenCommandTest(unittest.TestCase):  # 38
    SHIM = ('@ECHO off\r\nGOTO start\r\n:start\r\nSETLOCAL\r\n'
            'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
            '"%dp0%\\node_modules\\@bitkyc08\\opencodex\\bin\\ocx.mjs" %*\r\n')
    JSON_ARG = json.dumps({"claude-opus-5-5": "Opus 5.5", "q": 'say "hi"'}, ensure_ascii=False)

    def test_npm_shim_runs_node_directly(self):
        exe = r"C:\Users\A User\AppData\Roaming\npm\ocx.cmd"
        script = os.path.join(os.path.dirname(exe), r"node_modules\@bitkyc08\opencodex\bin\ocx.mjs")
        cmd = rm.build_ocx_command(exe, ["config", "set", "p", self.JSON_ARG], windows=True,
                                   read_text=lambda p: self.SHIM, exists=lambda p: p == script,
                                   which=lambda n: r"C:\Program Files\nodejs\node.exe")
        self.assertEqual(cmd, [r"C:\Program Files\nodejs\node.exe", script, "config", "set", "p", self.JSON_ARG])

    def test_unknown_cmd_goes_through_cmd_exe_with_explicit_quoting(self):
        cmd = rm.build_ocx_command(r"C:\Tools Dir\ocx.cmd", ["config", "set", "p", '{"a": "b c"}'], windows=True,
                                   read_text=lambda p: "@echo off", exists=lambda p: False, which=lambda n: None,
                                   comspec=r"C:\Windows\System32\cmd.exe")
        self.assertEqual(cmd, r'"C:\Windows\System32\cmd.exe" /d /s /c ""C:\Tools Dir\ocx.cmd" "config" "set" "p" '
                              r'"{""a"": ""b c""}""')
        with self.assertRaises(rm.OcxError):
            rm.build_ocx_command(r"C:\x\ocx.cmd", ["100%"], windows=True, read_text=lambda p: "",
                                 exists=lambda p: False, which=lambda n: None)

    def test_non_windows_and_exe_are_run_as_is(self):
        self.assertEqual(rm.build_ocx_command("/usr/bin/ocx", ["sync"], windows=False), ["/usr/bin/ocx", "sync"])
        self.assertEqual(rm.build_ocx_command(r"C:\x\ocx.exe", ["sync"], windows=True), [r"C:\x\ocx.exe", "sync"])

    def test_quota_skill_builds_the_same_commands(self):
        for exe, args, kw in [
            (r"C:\Users\A User\npm\ocx.cmd", ["provider", "quota", "--json"],
             dict(read_text=lambda p: self.SHIM, exists=lambda p: True, which=lambda n: "node")),
            (r"C:\Tools Dir\ocx.cmd", ["config", "set", "p", '{"a": "b"}'],
             dict(read_text=lambda p: "", exists=lambda p: False, which=lambda n: None, comspec="cmd.exe")),
            ("/usr/bin/ocx", ["sync"], dict()),
        ]:
            windows = exe.endswith(".cmd")
            self.assertEqual(rm.build_ocx_command(exe, args, windows=windows, **kw),
                             quota.build_ocx_command(exe, args, windows=windows, **kw))

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_fake_cmd_shim_round_trips_arguments_without_a_window(self):
        with tempfile.TemporaryDirectory(prefix="ocx shim ") as tmp:
            echo = Path(tmp, "echo args.py")
            echo.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
            shim = Path(tmp, "ocx.cmd")
            shim.write_text(f'@"{sys.executable}" "%~dp0echo args.py" %*\r\n', encoding="utf-8")
            args = ["config", "set", "providers.anthropic.modelDisplayNames", self.JSON_ARG, "a b", "back\\"]
            result = rm.run_hidden(rm.build_ocx_command(str(shim), args), timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), args)

    @unittest.skipUnless(os.name == "nt" and shutil.which("node"), "Windows with node only")
    def test_fake_npm_shim_runs_node_without_cmd(self):
        with tempfile.TemporaryDirectory(prefix="ocx npm ") as tmp:
            entry = Path(tmp, "node_modules", "@bitkyc08", "opencodex", "bin", "ocx.mjs")
            entry.parent.mkdir(parents=True)
            entry.write_text("console.log(JSON.stringify(process.argv.slice(2)))\n", encoding="utf-8")
            shim = Path(tmp, "ocx.cmd")
            shim.write_text(self.SHIM, encoding="utf-8")
            command = rm.build_ocx_command(str(shim), ["config", "set", "p", self.JSON_ARG])
            self.assertIsInstance(command, list)
            self.assertNotIn("cmd", os.path.basename(command[0]).lower())
            result = rm.run_hidden(command, timeout=60)
            self.assertEqual(json.loads(result.stdout), ["config", "set", "p", self.JSON_ARG])


class HostNativeApplyTest(unittest.TestCase):  # 40
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def runner(self, fail_on=None):
        def run_cmd(cmd):
            self.calls.append(cmd[1])
            if cmd[1] == fail_on:
                raise subprocess.CalledProcessError(1, cmd, stderr="denied")
            return subprocess.CompletedProcess(cmd, 0)
        return run_cmd

    def apply(self, run_cmd, wait):
        return host.run_host_native(run_cmd, wait, pythonw=r"C:\Py\pythonw.exe", apply_script=r"C:\r & d\apply.py",
                                    workdir=r"C:\r & d", user=r"PC\me", tmpdir=self.tmp)

    def test_task_is_created_run_and_deleted_on_success(self):
        self.assertEqual(self.apply(self.runner(), lambda log: 0), 0)
        self.assertEqual(self.calls, ["/Create", "/Run", "/End", "/Delete"])
        self.assertEqual(os.listdir(self.tmp), [])

    def test_task_is_deleted_when_apply_fails_or_times_out(self):
        self.assertEqual(self.apply(self.runner(), lambda log: 3), 3)
        self.assertEqual(self.calls[-1], "/Delete")
        self.calls.clear()

        def timeout(log):
            raise TimeoutError("apply did not finish")
        with self.assertRaises(TimeoutError):
            self.apply(self.runner(), timeout)
        self.assertEqual(self.calls, ["/Create", "/Run", "/End", "/Delete"])

    def test_task_is_deleted_even_when_starting_it_fails(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply(self.runner(fail_on="/Run"), lambda log: 0)
        self.assertEqual(self.calls, ["/Create", "/Run", "/End", "/Delete"])
        self.assertEqual(os.listdir(self.tmp), [])

    def test_task_definition_is_on_demand_windowless_and_escaped(self):
        xml = host.task_xml(r"C:\Py\pythonw.exe", r"C:\r & d\apply.py", r"C:\t\log.txt", r"C:\r & d", r"PC\me")
        self.assertNotIn("<Triggers>", xml)
        self.assertIn(r"<Command>C:\Py\pythonw.exe</Command>", xml)
        self.assertIn("&amp;", xml)
        self.assertNotIn("r & d", xml)

    def test_exit_code_is_read_from_the_apply_log(self):
        log = os.path.join(self.tmp, "log.txt")
        Path(log).write_text("set modelPickerOrder\nocx-extensions apply exit=0\n", encoding="utf-8")
        self.assertEqual(host.wait_for_exit(log, timeout=1, sleep=lambda s: None), 0)
        ticks = iter(range(100))
        with self.assertRaises(TimeoutError):
            host.wait_for_exit(os.path.join(self.tmp, "missing.txt"), timeout=3,
                               clock=lambda: next(ticks), sleep=lambda s: None)


class SchedulerTest(unittest.TestCase):
    def test_windows_task_is_one_named_task_overwritten_in_place(self):
        cmd = sched.windows_command(r"C:\Py\pythonw.exe", r"D:\repo\scripts\reconcile-models.py")
        self.assertEqual(cmd, sched.windows_command(r"C:\Py\pythonw.exe", r"D:\repo\scripts\reconcile-models.py"))
        self.assertEqual(cmd[cmd.index("/TN") + 1], sched.NAME)
        self.assertIn("/F", cmd)
        self.assertEqual(cmd[cmd.index("/MO") + 1], "15")

    def test_windows_task_never_runs_console_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp, "python.exe")
            python.write_bytes(b"")
            with self.assertRaises(OSError):
                sched.windows_python(str(python))
            Path(tmp, "pythonw.exe").write_bytes(b"")
            self.assertEqual(sched.windows_python(str(python)), str(Path(tmp, "pythonw.exe")))

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
