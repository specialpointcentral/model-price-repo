import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_prices  # noqa: E402


MANAGED_LUNA = {
    "input_cost_per_token": 1e-6,
    "output_cost_per_token": 6e-6,
    "long_context_input_token_threshold": 272000,
    "long_context_input_cost_multiplier": 2.0,
    "long_context_output_cost_multiplier": 1.5,
}


class PriceOverridesTest(unittest.TestCase):
    def write_json(self, root: Path, name: str, value) -> Path:
        path = root / name
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        return path

    def test_load_price_overrides_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                sync_prices.load_price_overrides(str(Path(tmp) / "missing.json"))

    def test_load_price_overrides_rejects_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "malformed.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                sync_prices.load_price_overrides(str(path))

    def test_load_price_overrides_rejects_invalid_structures(self):
        invalid_values = [
            [],
            {"": MANAGED_LUNA},
            {"gpt-5.6-luna": "not-an-object"},
            {"gpt-5.6-luna": {"mode": "chat"}},
            {"gpt-5.6-luna": {"input_cost_per_token": True}},
            {"gpt-5.6-luna": {"input_cost_per_token": -1}},
            {"gpt-5.6-luna": {"input_cost_per_token": float("inf")}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, value in enumerate(invalid_values):
                with self.subTest(value=value):
                    path = self.write_json(root, f"invalid-{index}.json", value)
                    with self.assertRaises(ValueError):
                        sync_prices.load_price_overrides(str(path))

    def test_apply_price_overrides_replaces_entry_without_mutating_input(self):
        original = {
            "gpt-5.6-luna": {
                "input_cost_per_token": 2e-7,
                "input_cost_per_token_above_272k_tokens": 4e-7,
            },
            "gpt-5.6-sol": {"input_cost_per_token": 5e-6},
        }
        result = sync_prices.apply_price_overrides(
            original, {"gpt-5.6-luna": MANAGED_LUNA}
        )

        self.assertEqual(MANAGED_LUNA, result["gpt-5.6-luna"])
        self.assertNotIn(
            "input_cost_per_token_above_272k_tokens",
            result["gpt-5.6-luna"],
        )
        self.assertEqual(
            {"input_cost_per_token": 5e-6}, result["gpt-5.6-sol"]
        )
        self.assertEqual(2e-7, original["gpt-5.6-luna"]["input_cost_per_token"])

    def test_main_applies_managed_entry_before_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "upstream_url": "https://example.invalid/prices.json",
                "output_file": "catalog.json",
                "hash_file": "catalog.sha256",
                "sync_mode": "full",
                "update_existing": True,
                "prefix_filters": ["gpt-"],
                "aliases": {
                    "codex-auto-review": {
                        "source": "gpt-5.6-luna",
                        "description": "managed Luna alias",
                    }
                },
            }
            config_path = self.write_json(root, "config.json", config)
            self.write_json(
                root, "price_overrides.json", {"gpt-5.6-luna": MANAGED_LUNA}
            )
            upstream = {
                "gpt-5.6-luna": {
                    "input_cost_per_token": 2e-7,
                    "output_cost_per_token": 1.2e-6,
                    "input_cost_per_token_above_272k_tokens": 4e-7,
                }
            }

            argv = [
                "sync_prices.py",
                "--config",
                config_path.name,
                "--repo-root",
                str(root),
            ]
            with mock.patch.object(sync_prices, "fetch_upstream", return_value=upstream):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        sync_prices.main()

            catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(MANAGED_LUNA, catalog["gpt-5.6-luna"])
            self.assertEqual(MANAGED_LUNA, catalog["codex-auto-review"])

    def test_repository_artifacts_match_managed_price_cards(self):
        overrides = sync_prices.load_price_overrides(
            str(ROOT / "price_overrides.json")
        )
        catalog_bytes = (ROOT / "model_prices_and_context_window.json").read_bytes()
        catalog = json.loads(catalog_bytes)
        stored_hash = (
            ROOT / "model_prices_and_context_window.sha256"
        ).read_text(encoding="utf-8").strip()
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

        expected = {
            "gpt-5.6-luna": {
                "input_cost_per_token": 1e-6,
                "input_cost_per_token_batches": 5e-7,
                "input_cost_per_token_flex": 5e-7,
                "input_cost_per_token_priority": 2e-6,
                "output_cost_per_token": 6e-6,
                "output_cost_per_token_batches": 3e-6,
                "output_cost_per_token_flex": 3e-6,
                "output_cost_per_token_priority": 1.2e-5,
                "cache_creation_input_token_cost": 1.25e-6,
                "cache_creation_input_token_cost_batches": 6.25e-7,
                "cache_creation_input_token_cost_flex": 6.25e-7,
                "cache_creation_input_token_cost_priority": 2.5e-6,
                "cache_read_input_token_cost": 1e-7,
                "cache_read_input_token_cost_flex": 5e-8,
                "cache_read_input_token_cost_priority": 2e-7,
            },
            "gpt-5.6-terra": {
                "input_cost_per_token": 2.5e-6,
                "input_cost_per_token_batches": 1.25e-6,
                "input_cost_per_token_flex": 1.25e-6,
                "input_cost_per_token_priority": 5e-6,
                "output_cost_per_token": 1.5e-5,
                "output_cost_per_token_batches": 7.5e-6,
                "output_cost_per_token_flex": 7.5e-6,
                "output_cost_per_token_priority": 3e-5,
                "cache_creation_input_token_cost": 3.125e-6,
                "cache_creation_input_token_cost_batches": 1.5625e-6,
                "cache_creation_input_token_cost_flex": 1.5625e-6,
                "cache_creation_input_token_cost_priority": 6.25e-6,
                "cache_read_input_token_cost": 2.5e-7,
                "cache_read_input_token_cost_flex": 1.25e-7,
                "cache_read_input_token_cost_priority": 5e-7,
            },
            "gpt-6-sol": {
                "input_cost_per_token": 3.5e-6,
                "input_cost_per_token_batches": 1.75e-6,
                "input_cost_per_token_flex": 1.75e-6,
                "input_cost_per_token_priority": 7e-6,
                "input_cost_per_token_above_272k_tokens": 7e-6,
                "input_cost_per_token_above_272k_tokens_batches": 3.5e-6,
                "input_cost_per_token_above_272k_tokens_flex": 3.5e-6,
                "input_cost_per_token_above_272k_tokens_priority": 1.4e-5,
                "output_cost_per_token": 2e-5,
                "output_cost_per_token_batches": 1e-5,
                "output_cost_per_token_flex": 1e-5,
                "output_cost_per_token_priority": 4e-5,
                "output_cost_per_token_above_272k_tokens": 3e-5,
                "output_cost_per_token_above_272k_tokens_batches": 1.5e-5,
                "output_cost_per_token_above_272k_tokens_flex": 1.5e-5,
                "output_cost_per_token_above_272k_tokens_priority": 6e-5,
                "cache_creation_input_token_cost": 4.2e-6,
                "cache_creation_input_token_cost_batches": 2.1e-6,
                "cache_creation_input_token_cost_flex": 2.1e-6,
                "cache_creation_input_token_cost_priority": 8.4e-6,
                "cache_creation_input_token_cost_above_272k_tokens": 8.4e-6,
                "cache_creation_input_token_cost_above_272k_tokens_batches": 4.2e-6,
                "cache_creation_input_token_cost_above_272k_tokens_flex": 4.2e-6,
                "cache_creation_input_token_cost_above_272k_tokens_priority": 1.68e-5,
                "cache_read_input_token_cost": 3.5e-7,
                "cache_read_input_token_cost_batches": 1.75e-7,
                "cache_read_input_token_cost_flex": 1.75e-7,
                "cache_read_input_token_cost_priority": 7e-7,
                "cache_read_input_token_cost_above_272k_tokens": 7e-7,
                "cache_read_input_token_cost_above_272k_tokens_batches": 3.5e-7,
                "cache_read_input_token_cost_above_272k_tokens_flex": 3.5e-7,
                "cache_read_input_token_cost_above_272k_tokens_priority": 1.4e-6,
            },
            "gpt-6-luna": {
                "input_cost_per_token": 1.5e-7,
                "input_cost_per_token_batches": 7.5e-8,
                "input_cost_per_token_flex": 7.5e-8,
                "input_cost_per_token_priority": 3e-7,
                "input_cost_per_token_above_272k_tokens": 3e-7,
                "input_cost_per_token_above_272k_tokens_batches": 1.5e-7,
                "input_cost_per_token_above_272k_tokens_flex": 1.5e-7,
                "input_cost_per_token_above_272k_tokens_priority": 6e-7,
                "output_cost_per_token": 1e-6,
                "output_cost_per_token_batches": 5e-7,
                "output_cost_per_token_flex": 5e-7,
                "output_cost_per_token_priority": 2e-6,
                "output_cost_per_token_above_272k_tokens": 1.5e-6,
                "output_cost_per_token_above_272k_tokens_batches": 7.5e-7,
                "output_cost_per_token_above_272k_tokens_flex": 7.5e-7,
                "output_cost_per_token_above_272k_tokens_priority": 3e-6,
                "cache_creation_input_token_cost": 2e-7,
                "cache_creation_input_token_cost_batches": 1e-7,
                "cache_creation_input_token_cost_flex": 1e-7,
                "cache_creation_input_token_cost_priority": 4e-7,
                "cache_creation_input_token_cost_above_272k_tokens": 4e-7,
                "cache_creation_input_token_cost_above_272k_tokens_batches": 2e-7,
                "cache_creation_input_token_cost_above_272k_tokens_flex": 2e-7,
                "cache_creation_input_token_cost_above_272k_tokens_priority": 8e-7,
                "cache_read_input_token_cost": 1.5e-8,
                "cache_read_input_token_cost_batches": 7.5e-9,
                "cache_read_input_token_cost_flex": 7.5e-9,
                "cache_read_input_token_cost_priority": 3e-8,
                "cache_read_input_token_cost_above_272k_tokens": 3e-8,
                "cache_read_input_token_cost_above_272k_tokens_batches": 1.5e-8,
                "cache_read_input_token_cost_above_272k_tokens_flex": 1.5e-8,
                "cache_read_input_token_cost_above_272k_tokens_priority": 6e-8,
            },
            "gpt-6-astra": {
                "input_cost_per_token": 1e-5,
                "input_cost_per_token_batches": 5e-6,
                "input_cost_per_token_flex": 5e-6,
                "input_cost_per_token_priority": 2e-5,
                "input_cost_per_token_above_272k_tokens": 2e-5,
                "input_cost_per_token_above_272k_tokens_flex": 1e-5,
                "input_cost_per_token_above_272k_tokens_priority": 4e-5,
                "output_cost_per_token": 5e-5,
                "output_cost_per_token_batches": 2.5e-5,
                "output_cost_per_token_flex": 2.5e-5,
                "output_cost_per_token_priority": 1e-4,
                "output_cost_per_token_above_272k_tokens": 7.5e-5,
                "output_cost_per_token_above_272k_tokens_flex": 3.75e-5,
                "output_cost_per_token_above_272k_tokens_priority": 1.5e-4,
                "cache_creation_input_token_cost": 1.25e-5,
                "cache_creation_input_token_cost_flex": 6.25e-6,
                "cache_creation_input_token_cost_priority": 2.5e-5,
                "cache_creation_input_token_cost_above_272k_tokens": 2.5e-5,
                "cache_creation_input_token_cost_above_272k_tokens_flex": 1.25e-5,
                "cache_creation_input_token_cost_above_272k_tokens_priority": 5e-5,
                "cache_read_input_token_cost": 2e-6,
                "cache_read_input_token_cost_flex": 1e-6,
                "cache_read_input_token_cost_priority": 4e-6,
                "cache_read_input_token_cost_above_272k_tokens": 4e-6,
                "cache_read_input_token_cost_above_272k_tokens_flex": 2e-6,
                "cache_read_input_token_cost_above_272k_tokens_priority": 8e-6,
            },
        }
        self.assertEqual(set(expected), set(overrides))
        self.assertTrue(
            set(overrides).isdisjoint(config.get("custom_models", {}))
        )
        for model, prices in expected.items():
            self.assertEqual(overrides[model], catalog[model])
            for field, value in prices.items():
                self.assertEqual(value, catalog[model][field])

        for model in ("gpt-6-luna", "gpt-6-sol"):
            for field in (
                "input_cost_per_token",
                "output_cost_per_token",
                "cache_creation_input_token_cost",
                "cache_read_input_token_cost",
            ):
                base = catalog[model][field]
                long_context_multiplier = (
                    1.5 if field == "output_cost_per_token" else 2
                )
                self.assertAlmostEqual(
                    base * 2,
                    catalog[model][field + "_priority"],
                    delta=1e-18,
                )
                self.assertAlmostEqual(
                    base * long_context_multiplier,
                    catalog[model][field + "_above_272k_tokens"],
                    delta=1e-18,
                )
                self.assertAlmostEqual(
                    base * long_context_multiplier * 2,
                    catalog[model][field + "_above_272k_tokens_priority"],
                    delta=1e-18,
                )

        for model in ("gpt-5.6-luna", "gpt-5.6-terra"):
            self.assertEqual(
                272000,
                catalog[model]["long_context_input_token_threshold"],
            )
            self.assertEqual(
                2.0, catalog[model]["long_context_input_cost_multiplier"]
            )
            self.assertEqual(
                1.5, catalog[model]["long_context_output_cost_multiplier"]
            )
            self.assertFalse(any("_above_" in key for key in catalog[model]))

        self.assertEqual(overrides["gpt-5.6-luna"], catalog["codex-auto-review"])
        self.assertEqual(hashlib.sha256(catalog_bytes).hexdigest(), stored_hash)


if __name__ == "__main__":
    unittest.main()
