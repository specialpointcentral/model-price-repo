import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sync_prices  # noqa: E402


class CatalogSyncRegressionTest(unittest.TestCase):
    def run_sync(self, config, existing, upstream, fallbacks=None, overrides=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            (root / "catalog.json").write_text(json.dumps(existing), encoding="utf-8")
            (root / "price_overrides.json").write_text(json.dumps(overrides or {}), encoding="utf-8")
            if fallbacks is not None:
                (root / "fallbacks.json").write_text(
                    json.dumps(fallbacks), encoding="utf-8"
                )
            argv = ["sync_prices.py", "--config", "config.json", "--repo-root", str(root)]
            with mock.patch.object(sync_prices, "fetch_upstream", return_value=upstream):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        sync_prices.main()
            return json.loads((root / "catalog.json").read_text(encoding="utf-8"))

    def config(self):
        return {
            "upstream_url": "https://example.invalid/catalog.json",
            "output_file": "catalog.json",
            "hash_file": "catalog.sha256",
            "sync_mode": "additive",
            "update_existing": False,
            "prefix_filters": ["gemini-", "gemini/gemini-4-", "claude-"],
        }

    def test_selected_upstream_prices_refresh_before_aliases(self):
        config = self.config()
        config["refresh_existing_prefixes"] = ["gemini/gemini-4-"]
        config["aliases"] = {"gemini-4-argon": {"source": "gemini/gemini-4-argon"}}
        existing = {
            "gemini/gemini-4-argon": {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5},
            "gemini-2.5-flash": {"input_cost_per_token": 3e-7},
        }
        upstream = {
            "gemini/gemini-4-argon": {"input_cost_per_token": 4e-6, "output_cost_per_token": 2e-5},
            "gemini-2.5-flash": {"input_cost_per_token": 5e-7},
        }
        catalog = self.run_sync(config, existing, upstream)
        self.assertEqual(4e-6, catalog["gemini-4-argon"]["input_cost_per_token"])
        self.assertEqual(2e-5, catalog["gemini-4-argon"]["output_cost_per_token"])
        self.assertEqual(3e-7, catalog["gemini-2.5-flash"]["input_cost_per_token"])

    def test_existing_upstream_alias_follows_its_new_target(self):
        config = self.config()
        existing = {"claude-fable-latest": {"input_cost_per_token": 1e-5, "output_cost_per_token": 5e-5}}
        upstream = {
            "claude-fable-latest": {"alias_of": "claude-fable-5-2"},
            "claude-fable-5-2": {"input_cost_per_token": 8e-6, "output_cost_per_token": 4e-5},
        }
        catalog = self.run_sync(config, existing, upstream)
        self.assertEqual(8e-6, catalog["claude-fable-latest"]["input_cost_per_token"])
        self.assertEqual(4e-5, catalog["claude-fable-latest"]["output_cost_per_token"])

    def test_explicit_custom_alias_price_takes_precedence_over_upstream_alias(self):
        config = self.config()
        config["custom_models"] = {
            "claude-fable-latest": {"input_cost_per_token": 7e-6, "output_cost_per_token": 3e-5}
        }
        upstream = {
            "claude-fable-latest": {"alias_of": "claude-fable-5-2"},
            "claude-fable-5-2": {"input_cost_per_token": 8e-6, "output_cost_per_token": 4e-5},
        }
        existing = {"claude-fable-latest": {"input_cost_per_token": 1e-5, "output_cost_per_token": 5e-5}}
        catalog = self.run_sync(config, existing, upstream)
        self.assertEqual(7e-6, catalog["claude-fable-latest"]["input_cost_per_token"])
        self.assertEqual(3e-5, catalog["claude-fable-latest"]["output_cost_per_token"])

    def test_partial_custom_alias_inherits_unmodified_source_prices(self):
        config = self.config()
        config["custom_models"] = {"claude-fable-latest": {"input_cost_per_token": 7e-6}}
        upstream = {
            "claude-fable-latest": {"alias_of": "claude-fable-5-2"},
            "claude-fable-5-2": {
                "input_cost_per_token": 8e-6, "output_cost_per_token": 4e-5,
                "cache_read_input_token_cost": 2e-7,
            },
        }
        catalog = self.run_sync(config, {}, upstream)
        self.assertEqual(7e-6, catalog["claude-fable-latest"]["input_cost_per_token"])
        self.assertIn("output_cost_per_token", catalog["claude-fable-latest"])
        self.assertIn("cache_read_input_token_cost", catalog["claude-fable-latest"])
        self.assertEqual(4e-5, catalog["claude-fable-latest"]["output_cost_per_token"])
        self.assertEqual(2e-7, catalog["claude-fable-latest"]["cache_read_input_token_cost"])
        self.assertEqual({"alias_of": "claude-fable-5-2"}, upstream["claude-fable-latest"])

    def test_upstream_alias_chain_uses_managed_intermediate_price(self):
        config = self.config()
        upstream = {
            "claude-latest": {"alias_of": "claude-managed"},
            "claude-managed": {"alias_of": "claude-base"},
            "claude-base": {"input_cost_per_token": 8e-6, "output_cost_per_token": 4e-5},
        }
        overrides = {"claude-managed": {"input_cost_per_token": 4e-6, "output_cost_per_token": 2e-5}}
        catalog = self.run_sync(config, {}, upstream, overrides=overrides)
        self.assertEqual(4e-6, catalog["claude-latest"]["input_cost_per_token"])
        self.assertEqual(2e-5, catalog["claude-latest"]["output_cost_per_token"])

    def test_managed_price_for_configured_alias_is_not_overwritten(self):
        config = self.config()
        config["aliases"] = {"gemini-4-argon": {"source": "gemini/gemini-4-argon"}}
        upstream = {"gemini/gemini-4-argon": {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5}}
        overrides = {"gemini-4-argon": {"input_cost_per_token": 4e-6, "output_cost_per_token": 2e-5}}
        catalog = self.run_sync(config, {}, upstream, overrides=overrides)
        self.assertEqual(4e-6, catalog["gemini-4-argon"]["input_cost_per_token"])
        self.assertEqual(2e-5, catalog["gemini-4-argon"]["output_cost_per_token"])

    def test_configured_alias_applies_partial_custom_after_inheriting_source(self):
        config = self.config()
        config["aliases"] = {"gemini-4-argon": {"source": "gemini/gemini-4-argon"}}
        config["custom_models"] = {"gemini-4-argon": {"input_cost_per_token": 3e-6}}
        upstream = {"gemini/gemini-4-argon": {
            "input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5,
            "cache_read_input_token_cost": 1e-7,
        }}
        catalog = self.run_sync(config, {}, upstream)
        self.assertEqual(3e-6, catalog["gemini-4-argon"]["input_cost_per_token"])
        self.assertEqual(1e-5, catalog["gemini-4-argon"]["output_cost_per_token"])
        self.assertEqual(1e-7, catalog["gemini-4-argon"]["cache_read_input_token_cost"])

    def test_missing_required_source_aborts_sync(self):
        config = self.config()
        config["required_price_sources"] = ["claude-fable-5-2"]
        with self.assertRaisesRegex(ValueError, "claude-fable-5-2"):
            self.run_sync(config, {}, {})

    def test_unresolved_required_upstream_alias_aborts_sync(self):
        config = self.config()
        config["required_upstream_aliases"] = ["claude-fable-latest"]
        existing = {"claude-fable-latest": {"input_cost_per_token": 8e-6, "output_cost_per_token": 4e-5}}
        upstream = {"claude-fable-latest": {"alias_of": "vertex_ai/claude-fable-5-2"}}
        with self.assertRaisesRegex(ValueError, "claude-fable-latest"):
            self.run_sync(config, existing, upstream)

    def test_upstream_alias_uses_fresh_configured_alias_source(self):
        config = self.config()
        config["refresh_existing_prefixes"] = ["gemini/gemini-4-"]
        config["aliases"] = {"gemini-4-argon": {"source": "gemini/gemini-4-argon"}}
        existing = {
            "gemini-4-latest": {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5},
            "gemini-4-argon": {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5},
        }
        upstream = {
            "gemini-4-latest": {"alias_of": "gemini-4-argon"},
            "gemini/gemini-4-argon": {"input_cost_per_token": 4e-6, "output_cost_per_token": 2e-5},
        }
        catalog = self.run_sync(config, existing, upstream)
        self.assertEqual(4e-6, catalog["gemini-4-latest"]["input_cost_per_token"])
        self.assertEqual(catalog["gemini-4-argon"], catalog["gemini-4-latest"])

    def test_reintroduced_historical_alias_is_stable_on_first_and_second_sync(self):
        config = self.config()
        config["legacy_fallback_file"] = "fallbacks.json"
        upstream = {
            "gemini-2.0-flash": {"alias_of": "gemini/gemini-4-argon"},
            "gemini/gemini-4-argon": {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5},
        }
        fallbacks = {"gemini-2.0-flash": {"input_cost_per_token": 1e-7, "output_cost_per_token": 4e-7}}
        first = self.run_sync(config, {}, upstream, fallbacks)
        second = self.run_sync(config, first, upstream, fallbacks)
        self.assertEqual(2e-6, first["gemini-2.0-flash"]["input_cost_per_token"])
        self.assertEqual(first, second)
        self.assertEqual({"alias_of": "gemini/gemini-4-argon"}, upstream["gemini-2.0-flash"])

    def test_incomplete_selected_upstream_price_does_not_replace_catalog(self):
        config = self.config()
        config["refresh_existing_prefixes"] = ["gemini/gemini-4-"]
        config["required_price_fields"] = {
            "gemini/gemini-4-": ["input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost"]
        }
        existing = {"gemini/gemini-4-argon": {
            "input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5, "cache_read_input_token_cost": 1e-7,
        }}
        upstream = {"gemini/gemini-4-argon": {"input_cost_per_token": 4e-6, "cache_read_input_token_cost": 2e-7}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            original = json.dumps(existing)
            (root / "catalog.json").write_text(original, encoding="utf-8")
            (root / "catalog.sha256").write_text("prior-hash\n", encoding="utf-8")
            (root / "price_overrides.json").write_text("{}\n", encoding="utf-8")
            argv = ["sync_prices.py", "--repo-root", str(root)]
            with mock.patch.object(sync_prices, "fetch_upstream", return_value=upstream):
                with mock.patch.object(sys, "argv", argv):
                    with self.assertRaisesRegex(ValueError, "output_cost_per_token"):
                        sync_prices.main()
            self.assertEqual(original, (root / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual("prior-hash\n", (root / "catalog.sha256").read_text(encoding="utf-8"))

    def test_unreviewed_token_price_does_not_replace_catalog(self):
        name = "gemini/gemini-4-argon"
        config = self.config()
        config["require_token_price_overrides"] = [name]
        config["custom_models"] = {name: {"input_cost_per_token": 4e-6}}
        upstream = {name: {"input_cost_per_token": 2e-6, "input_cost_per_audio_token": 2e-6}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            original = json.dumps({name: {"input_cost_per_token": 4e-6}})
            (root / "catalog.json").write_text(original, encoding="utf-8")
            (root / "catalog.sha256").write_text("prior-hash\n", encoding="utf-8")
            (root / "price_overrides.json").write_text("{}\n", encoding="utf-8")
            argv = ["sync_prices.py", "--repo-root", str(root)]
            with mock.patch.object(sync_prices, "fetch_upstream", return_value=upstream):
                with mock.patch.object(sys, "argv", argv):
                    with self.assertRaisesRegex(ValueError, "input_cost_per_audio_token"):
                        sync_prices.main()
            self.assertEqual(original, (root / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual("prior-hash\n", (root / "catalog.sha256").read_text(encoding="utf-8"))

    def test_invalid_new_custom_token_price_is_rejected(self):
        name = "gemini/gemini-4-argon"
        config = self.config()
        config["require_token_price_overrides"] = [name]
        config["custom_models"] = {name: {
            "input_cost_per_token": 4e-6, "input_cost_per_video_token": -1,
        }}
        upstream = {name: {"input_cost_per_token": 2e-6}}
        with self.assertRaisesRegex(ValueError, "input_cost_per_video_token"):
            self.run_sync(config, {}, upstream)

    def test_historical_fallbacks_restore_only_missing_models_and_fields(self):
        config = self.config()
        config["legacy_fallback_file"] = "fallbacks.json"
        existing = {"gemini-3-pro-preview": {"input_cost_per_token": 4e-6}}
        fallbacks = {
            "gemini-3-pro-preview": {"input_cost_per_token": 2e-6, "output_cost_per_token": 12e-6},
            "gemini-2.0-flash": {"input_cost_per_token": 1e-7, "output_cost_per_token": 4e-7},
        }
        catalog = self.run_sync(config, existing, {}, fallbacks)
        self.assertEqual(4e-6, catalog["gemini-3-pro-preview"]["input_cost_per_token"])
        self.assertIn("output_cost_per_token", catalog["gemini-3-pro-preview"])
        self.assertEqual(12e-6, catalog["gemini-3-pro-preview"]["output_cost_per_token"])
        self.assertIn("gemini-2.0-flash", catalog)
        self.assertEqual(1e-7, catalog["gemini-2.0-flash"]["input_cost_per_token"])

    def test_repo_configuration_accepts_and_names_gemini_4_sources(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        upstream = {
            "gemini/gemini-4-argon": {"input_cost_per_token": 2e-6},
            "gemini/gemini-4-carbon": {"input_cost_per_token": 2e-6},
            "vertex_ai/gemini-4-argon-cyber-permissive": {"input_cost_per_token": 4e-6},
            "vertex_ai/gemini-4-carbon-cyber-permissive": {"input_cost_per_token": 4e-6},
        }
        selected = sync_prices.filter_upstream(upstream, config)
        result = sync_prices.apply_aliases(selected, config["aliases"])
        for name in (
            "gemini-4-argon", "gemini-4-carbon",
            "gemini-4-argon-cyber-permissive", "gemini-4-carbon-cyber-permissive",
            "gemini-4-argon-low", "gemini-4-argon-medium", "gemini-4-argon-high",
            "gemini-4-carbon-low", "gemini-4-carbon-medium", "gemini-4-carbon-high",
        ):
            with self.subTest(model=name):
                self.assertIn(name, result)

    def test_repo_gemini_4_standard_prices_survive_upstream_refresh(self):
        repo_config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        sources = {
            f"{provider}/gemini-4-{model}"
            for provider in ("gemini", "vertex_ai")
            for model in ("argon", "carbon")
        }
        config = self.config()
        config["prefix_filters"].append("vertex_ai/gemini-4-")
        config["refresh_existing_prefixes"] = repo_config["refresh_existing_prefixes"]
        config["aliases"] = {
            name: alias for name, alias in repo_config["aliases"].items()
            if name.startswith("gemini-4-")
        }
        config["custom_models"] = {
            name: card for name, card in repo_config["custom_models"].items()
            if name in sources
        }
        config["require_token_price_overrides"] = repo_config.get("require_token_price_overrides", [])
        config["required_price_sources"] = sorted(sources)
        config["required_price_fields"] = {
            prefix: fields for prefix, fields in repo_config["required_price_fields"].items()
            if prefix.startswith(("gemini/gemini-4-", "vertex_ai/gemini-4-"))
        }

        promo = {
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 1e-5,
            "cache_read_input_token_cost": 1e-7,
            "input_cost_per_token_batches": 1e-6,
            "output_cost_per_token_batches": 5e-6,
            "cache_read_input_token_cost_batches": 5e-8,
            "input_cost_per_token_flex": 1e-6,
            "output_cost_per_token_flex": 5e-6,
            "cache_read_input_token_cost_flex": 5e-8,
            "input_cost_per_token_priority": 3.6e-6,
            "output_cost_per_token_priority": 1.8e-5,
            "cache_read_input_token_cost_priority": 1.8e-7,
            "output_cost_per_reasoning_token": 1e-5,
            "google_maps_grounding_cost_per_query": 0.014,
            "supports_reasoning": True,
            "max_input_tokens": 1000000,
        }
        standard = {
            "input_cost_per_token": 4e-6,
            "output_cost_per_token": 2e-5,
            "cache_read_input_token_cost": 2e-7,
            "input_cost_per_token_batches": 2e-6,
            "output_cost_per_token_batches": 1e-5,
            "cache_read_input_token_cost_batches": 1e-7,
            "input_cost_per_token_flex": 2e-6,
            "output_cost_per_token_flex": 1e-5,
            "cache_read_input_token_cost_flex": 1e-7,
            "input_cost_per_token_priority": 7.2e-6,
            "output_cost_per_token_priority": 3.6e-5,
            "cache_read_input_token_cost_priority": 3.6e-7,
            "output_cost_per_reasoning_token": 2e-5,
        }
        audio_promo = {
            "input_cost_per_audio_token": 2e-6,
            "input_cost_per_audio_token_batches": 1e-6,
            "input_cost_per_audio_token_priority": 3.6e-6,
        }
        audio_standard = {
            "input_cost_per_audio_token": 4e-6,
            "input_cost_per_audio_token_batches": 2e-6,
            "input_cost_per_audio_token_priority": 7.2e-6,
        }
        upstream = {}
        for name in sources:
            upstream[name] = copy.deepcopy(promo)
            if name.startswith("vertex_ai/"):
                upstream[name].update(audio_promo)
            if name == "vertex_ai/gemini-4-carbon":
                for field in tuple(upstream[name]):
                    if field.endswith("_flex"):
                        del upstream[name][field]
        for model in ("argon", "carbon"):
            cyber = f"vertex_ai/gemini-4-{model}-cyber-permissive"
            upstream[cyber] = {"input_cost_per_token": 4e-6, "output_cost_per_token": 2e-5,
                               "cache_read_input_token_cost": 2e-7, "supports_reasoning": True}
        unrelated = {"input_cost_per_token": 2e-6, "cache_read_input_token_cost": 1e-7}
        upstream["claude-sonnet-5-5"] = copy.deepcopy(unrelated)

        first = self.run_sync(config, {}, upstream)
        upgraded_upstream = copy.deepcopy(upstream)
        for name in sources:
            upgraded_upstream[name].update({k: v for k, v in standard.items() if k in upstream[name]})
            if name.startswith("vertex_ai/"):
                upgraded_upstream[name].update(audio_standard)
            upgraded_upstream[name]["max_input_tokens"] = 2000000
        second = self.run_sync(config, first, upgraded_upstream)
        vertex_carbon = "vertex_ai/gemini-4-carbon"
        new_fields = (
            (vertex_carbon, "input_cost_per_token_flex", 1e-6),
            ("gemini/gemini-4-argon", "input_cost_per_audio_token", 2e-6),
            ("gemini/gemini-4-carbon", "input_cost_per_audio_token", 2e-6),
            (vertex_carbon, "input_cost_per_image_token", 2e-6),
        )
        for name, field, price in new_fields:
            with self.subTest(new_field=field, model=name):
                changed_upstream = copy.deepcopy(upgraded_upstream)
                changed_upstream[name][field] = price
                with self.assertRaisesRegex(ValueError, field):
                    self.run_sync(config, second, changed_upstream)

        for catalog, limit in ((first, 1000000), (second, 2000000)):
            for name in sources:
                with self.subTest(model=name, limit=limit):
                    for field, price in standard.items():
                        if field in upstream[name]:
                            self.assertEqual(price, catalog[name][field], field)
                        else:
                            self.assertNotIn(field, catalog[name])
                    if name.startswith("vertex_ai/"):
                        for field, price in audio_standard.items():
                            self.assertEqual(price, catalog[name][field], field)
                    self.assertEqual(limit, catalog[name]["max_input_tokens"])
                    self.assertTrue(catalog[name]["supports_reasoning"])
                    self.assertEqual(0.014, catalog[name]["google_maps_grounding_cost_per_query"])
            for model in ("argon", "carbon"):
                source = catalog[f"gemini/gemini-4-{model}"]
                for suffix in ("", "-low", "-medium", "-high"):
                    self.assertEqual(source, catalog[f"gemini-4-{model}{suffix}"])
                cyber = f"vertex_ai/gemini-4-{model}-cyber-permissive"
                self.assertEqual(upstream[cyber], catalog[cyber])
                self.assertEqual(upstream[cyber], catalog[f"gemini-4-{model}-cyber-permissive"])
            self.assertEqual(unrelated, catalog["claude-sonnet-5-5"])

    def test_repo_configuration_refreshes_fable_52_price_changes(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        existing = {"claude-fable-5-2": {"input_cost_per_token": 8e-6}}
        upstream = {"claude-fable-5-2": {"input_cost_per_token": 9e-6}}
        merged, _ = sync_prices.merge_models(
            existing, upstream, config["sync_mode"], config["update_existing"],
            tuple(config.get("refresh_existing_prefixes", [])),
        )
        self.assertEqual(9e-6, merged["claude-fable-5-2"]["input_cost_per_token"])

    def test_managed_gemini_base_price_flows_to_thinking_level_aliases(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        data = {
            "gemini/gemini-4-argon": {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5},
            "gemini-4-argon": {"input_cost_per_token": 4e-6, "output_cost_per_token": 2e-5},
        }
        result = sync_prices.apply_aliases(data, config["aliases"], {"gemini-4-argon"})
        for level in ("low", "medium", "high"):
            with self.subTest(level=level):
                self.assertEqual(4e-6, result["gemini-4-argon-" + level]["input_cost_per_token"])

    def test_fable_price_validation_rejects_missing_cache_write_price(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        incomplete = {"claude-fable-5-2": {
            "input_cost_per_token": 8e-6, "output_cost_per_token": 4e-5,
            "cache_read_input_token_cost": 2e-7,
        }}
        with self.assertRaisesRegex(ValueError, "cache_creation_input_token_cost"):
            sync_prices.validate_required_price_fields(incomplete, config["required_price_fields"])

    def test_fresh_rebuild_retains_legacy_gemini_image_model(self):
        config = self.config()
        config["legacy_fallback_file"] = "fallbacks.json"
        fallbacks = json.loads((ROOT / "legacy_gemini_prices.json").read_text(encoding="utf-8"))
        catalog = self.run_sync(config, {}, {}, fallbacks)
        self.assertIn("gemini-2.0-flash-exp-image-generation", catalog)
        self.assertIn("input_cost_per_token", catalog["gemini-2.0-flash-exp-image-generation"])
        self.assertIn("output_cost_per_token", catalog["gemini-2.0-flash-exp-image-generation"])


class RebuildTest(unittest.TestCase):
    def prepare(self, root: Path, upstream_url: str):
        (root / "scripts").mkdir()
        shutil.copy2(ROOT / "rebuild.sh", root / "rebuild.sh")
        shutil.copy2(ROOT / "scripts" / "sync_prices.py", root / "scripts" / "sync_prices.py")
        config = {
            "upstream_url": upstream_url,
            "output_file": "model_prices_and_context_window.json",
            "hash_file": "model_prices_and_context_window.sha256",
            "sync_mode": "additive",
            "prefix_filters": ["gemini-"],
        }
        (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        (root / "price_overrides.json").write_text("{}", encoding="utf-8")

    def test_failed_rebuild_preserves_existing_catalog_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare(root, (root / "missing-upstream.json").as_uri())
            catalog = root / "model_prices_and_context_window.json"
            digest = root / "model_prices_and_context_window.sha256"
            catalog.write_bytes(b'old catalog\n')
            digest.write_bytes(b'old hash\n')
            result = subprocess.run(["bash", str(root / "rebuild.sh")], capture_output=True)
            self.assertNotEqual(0, result.returncode)
            self.assertTrue(catalog.exists())
            self.assertTrue(digest.exists())
            self.assertEqual(b'old catalog\n', catalog.read_bytes())
            self.assertEqual(b'old hash\n', digest.read_bytes())

    def test_successful_rebuild_replaces_catalog_and_matching_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "upstream.json"
            source.write_text(json.dumps({"gemini-test": {"input_cost_per_token": 2e-6}}))
            self.prepare(root, source.as_uri())
            catalog = root / "model_prices_and_context_window.json"
            digest = root / "model_prices_and_context_window.sha256"
            catalog.write_text("old catalog\n")
            digest.write_text("old hash\n")
            result = subprocess.run(["bash", str(root / "rebuild.sh")], capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr.decode())
            self.assertEqual(2e-6, json.loads(catalog.read_text())["gemini-test"]["input_cost_per_token"])
            self.assertEqual(hashlib.sha256(catalog.read_bytes()).hexdigest(), digest.read_text().strip())


if __name__ == "__main__":
    unittest.main()
