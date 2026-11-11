#!/usr/bin/env python3
"""Sync model pricing from upstream litellm, applying prefix filters,
aliases, and custom model definitions.

Usage:
    python3 scripts/sync_prices.py --config config.json --repo-root .
"""

import argparse
import copy
from decimal import Decimal
import hashlib
import json
import logging
import math
import os
import sys
import urllib.error
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REQUIRED_CONFIG_KEYS = [
    "upstream_url",
    "output_file",
    "hash_file",
    "sync_mode",
    "prefix_filters",
]


def load_config(path: str) -> dict:
    """Read and validate config.json."""
    if not os.path.isfile(path):
        log.error("Config file not found: %s", path)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in cfg]
    if missing:
        log.error("Config missing required keys: %s", ", ".join(missing))
        sys.exit(1)
    if cfg["sync_mode"] not in ("additive", "full"):
        log.error("Invalid sync_mode '%s'; must be 'additive' or 'full'", cfg["sync_mode"])
        sys.exit(1)
    return cfg


# ---------------------------------------------------------------------------
# Existing data
# ---------------------------------------------------------------------------


def load_existing(path: str) -> dict:
    """Load the current output file, or return {} on first run."""
    if not os.path.isfile(path):
        log.info("No existing output file; starting fresh.")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_existing_hash(path: str) -> str:
    """Read the stored SHA-256 hex digest, or return empty string."""
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


# ---------------------------------------------------------------------------
# Upstream fetch
# ---------------------------------------------------------------------------


def fetch_upstream(url: str) -> dict:
    """Download the full upstream pricing JSON."""
    log.info("Fetching upstream: %s", url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "model-price-repo/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError) as exc:
        log.error("Failed to fetch upstream: %s", exc)
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("Upstream JSON is invalid: %s", exc)
        sys.exit(1)

    if not isinstance(data, dict):
        log.error("Upstream JSON is not an object (got %s)", type(data).__name__)
        sys.exit(1)

    log.info("Upstream contains %d model entries.", len(data))
    return data


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def filter_upstream(data: dict, config: dict) -> dict:
    """Apply prefix_filters and exclude_patterns to upstream data."""
    prefixes = tuple(config.get("prefix_filters", []))
    excludes = config.get("exclude_patterns", [])

    filtered = {}
    for key, value in data.items():
        # Exclude first
        if any(pat in key for pat in excludes):
            continue
        # Then check prefix match
        if prefixes and not key.startswith(prefixes):
            continue
        filtered[key] = value

    log.info("Filtered to %d models (from %d upstream).", len(filtered), len(data))
    return filtered


def validate_required_price_fields(
    data: dict, rules: dict, required_sources=(), allow_aliases: bool = False,
) -> None:
    """Reject incomplete upstream price cards before they can overwrite the catalog."""
    if not isinstance(rules, dict):
        raise ValueError("required_price_fields must be a JSON object")
    if not isinstance(required_sources, (list, tuple)) or not all(
        isinstance(name, str) and name for name in required_sources
    ):
        raise ValueError("required_price_sources must be a list of model names")
    for name in required_sources:
        if name not in data:
            raise ValueError(f"Required upstream price source missing: {name}")
    for prefix, fields in rules.items():
        if not isinstance(prefix, str) or not prefix or not isinstance(fields, list) or not all(
            isinstance(field, str) and field for field in fields
        ):
            raise ValueError("required_price_fields must map prefixes to lists of field names")
        for model, entry in data.items():
            if not model.startswith(prefix):
                continue
            if allow_aliases and model not in required_sources and isinstance(entry, dict) and set(entry) == {"alias_of"}:
                continue
            for field in fields:
                value = entry.get(field) if isinstance(entry, dict) else None
                if not _is_valid_base_token_price(value) or value <= 0:
                    raise ValueError(f"Incomplete upstream pricing for {model!r}: {field}")


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def merge_models(
    existing: dict,
    filtered: dict,
    sync_mode: str,
    update_existing: bool,
    refresh_existing_prefixes: tuple[str, ...] = (),
) -> tuple[dict, dict]:
    """Merge filtered upstream into existing data.

    Returns (merged_dict, stats_dict).
    """
    stats = {"added": 0, "updated": 0, "unchanged": 0, "total_upstream": len(filtered)}

    if sync_mode == "full":
        # Full mode: replace entirely with filtered upstream
        stats["added"] = len(filtered)
        return copy.deepcopy(filtered), stats

    # Additive mode
    merged = copy.deepcopy(existing)
    for key, value in filtered.items():
        if key not in merged:
            merged[key] = copy.deepcopy(value)
            stats["added"] += 1
        elif update_existing or key.startswith(refresh_existing_prefixes):
            if merged[key] != value:
                merged[key] = copy.deepcopy(value)
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1
        else:
            # update_existing=False: preserve existing fields, but absorb new fields from upstream
            if isinstance(merged[key], dict) and isinstance(value, dict):
                if set(merged[key]) == {"alias_of"} and set(value) == {"alias_of"}:
                    if merged[key] != value:
                        merged[key] = copy.deepcopy(value)
                        stats["updated"] += 1
                    else:
                        stats["unchanged"] += 1
                    continue
                new_fields = {k: copy.deepcopy(v) for k, v in value.items() if k not in merged[key]}
                if new_fields:
                    merged[key].update(new_fields)
                    log.info("Model '%s': absorbed %d new field(s) from upstream: %s", key, len(new_fields), list(new_fields))
                    stats["updated"] += 1
                else:
                    stats["unchanged"] += 1
            else:
                stats["unchanged"] += 1

    return merged, stats


def apply_legacy_fallbacks(data: dict, path: str) -> dict:
    """Restore historical models and fields only when they are missing."""
    with open(path, "r", encoding="utf-8") as f:
        fallbacks = json.load(f)
    if not isinstance(fallbacks, dict) or any(
        not isinstance(name, str) or not isinstance(entry, dict)
        for name, entry in fallbacks.items()
    ):
        raise ValueError("Legacy fallback models must be a JSON object of model objects")

    for model, fallback in fallbacks.items():
        if model not in data:
            data[model] = copy.deepcopy(fallback)
        elif isinstance(data[model], dict):
            for field, value in fallback.items():
                if field not in data[model]:
                    data[model][field] = copy.deepcopy(value)
    log.info("Applied historical fallback data for %d models.", len(fallbacks))
    return data


# ---------------------------------------------------------------------------
# Aliases & custom models
# ---------------------------------------------------------------------------


def apply_aliases(
    data: dict, aliases: dict, protected: set[str] | None = None, *,
    upstream: dict | None = None, custom: dict | None = None,
    required_upstream_aliases=(),
) -> dict:
    """Resolve configured and upstream alias chains from final target prices."""
    protected = protected or set()
    custom = custom or {}
    upstream_aliases = {
        name: entry["alias_of"] for name, entry in upstream.items()
        if isinstance(entry, dict) and set(entry) == {"alias_of"}
        and isinstance(entry["alias_of"], str)
    } if upstream is not None else {}
    if not isinstance(required_upstream_aliases, (list, tuple)) or not all(
        isinstance(name, str) and name for name in required_upstream_aliases
    ):
        raise ValueError("required_upstream_aliases must be a list of model names")
    for name in required_upstream_aliases:
        if name not in upstream_aliases and name not in protected:
            raise ValueError(f"Required upstream alias missing: {name}")
    graph = dict(upstream_aliases)
    graph.update({name: cfg.get("source", "") for name, cfg in aliases.items()})

    def resolve(name: str, visited: set[str]):
        if name in visited:
            return None
        if name in protected:
            return copy.deepcopy(data.get(name))
        if name in graph:
            target = graph[name]
            if not isinstance(target, str) or not target:
                return None
            source = resolve(target, visited | {name})
            if not isinstance(source, dict):
                return None
            result = copy.deepcopy(source)
            if isinstance(custom.get(name), dict):
                result.update(copy.deepcopy(custom[name]))
            return result
        source = data.get(name)
        return copy.deepcopy(source) if isinstance(source, dict) and set(source) != {"alias_of"} else None

    for alias, target in graph.items():
        if alias in protected:
            continue
        resolved = resolve(alias, set())
        if resolved is not None:
            data[alias] = resolved
            log.info("Alias '%s' -> '%s' materialized.", alias, target)
        elif alias in required_upstream_aliases:
            raise ValueError(f"Required upstream alias cannot be resolved: {alias}")
        else:
            log.warning("Alias '%s': source model '%s' not found; skipping.", alias, target)
    return data


def apply_custom_models(data: dict, custom: dict) -> dict:
    """Inject custom model definitions (deep merge for existing, full set for new)."""
    for key, value in custom.items():
        if key in data and isinstance(data[key], dict) and isinstance(value, dict):
            data[key].update(value)
            log.info("Custom model '%s' merged (deep).", key)
        else:
            data[key] = value
            log.info("Custom model '%s' injected.", key)
    return data


BASE_TOKEN_PRICE_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
)


def _is_valid_base_token_price(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def validate_token_price_overrides(data: dict, models: list, custom: dict) -> None:
    """Require reviewed local values for every upstream token-price field."""
    for model in models:
        entry = data.get(model)
        overrides = custom.get(model)
        if not isinstance(entry, dict) or not isinstance(overrides, dict):
            raise ValueError(f"Missing upstream entry or token price overrides for {model}")
        fields = {field for field in entry if "cost" in field and "token" in field}
        missing = sorted(fields - overrides.keys())
        if missing:
            raise ValueError(f"Unreviewed token price fields for {model}: {', '.join(missing)}")
        invalid = sorted(
            field for field, value in overrides.items()
            if "cost" in field and "token" in field and not _is_valid_base_token_price(value)
        )
        if invalid:
            raise ValueError(f"Invalid token price overrides for {model}: {', '.join(invalid)}")


def load_price_overrides(path: str) -> dict:
    """Load and validate the required site-managed model entries."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Price override file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        overrides = json.load(f)
    if not isinstance(overrides, dict):
        raise ValueError("Price overrides root must be a JSON object")

    validated = {}
    for model, entry in overrides.items():
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Price override model names must be non-empty strings")
        if not isinstance(entry, dict):
            raise ValueError(f"Price override for {model!r} must be a JSON object")
        if not any(
            _is_valid_base_token_price(entry.get(field))
            for field in BASE_TOKEN_PRICE_FIELDS
        ):
            raise ValueError(
                f"Price override for {model!r} has no valid base token price"
            )
        validated[model.strip()] = entry
    return validated


def apply_price_overrides(data: dict, overrides: dict) -> dict:
    """Return a catalog with managed entries replaced wholesale."""
    merged = dict(data)
    for model, entry in overrides.items():
        merged[model] = copy.deepcopy(entry)
        log.info("Price override '%s' pinned.", model)
    return merged


def fill_cache_1hr_pricing(data: dict, config: dict) -> int:
    """Auto-fill missing cache_creation_input_token_cost_above_1hr for matching models.

    Uses a fixed ratio (default 1.6x) of the 5-minute cache write cost.
    Returns the number of models auto-filled.
    """
    auto_fill_cfg = config.get("cache_1hr_auto_fill")
    if not auto_fill_cfg:
        return 0

    prefix = auto_fill_cfg.get("model_prefix", "claude-")
    ratio = auto_fill_cfg.get("ratio", 1.6)
    count = 0

    for key, value in data.items():
        if not key.startswith(prefix):
            continue
        if not isinstance(value, dict):
            continue
        cost_5m = value.get("cache_creation_input_token_cost")
        if cost_5m is None:
            continue
        if value.get("cache_creation_input_token_cost_above_1hr") is not None:
            continue
        value["cache_creation_input_token_cost_above_1hr"] = float(
            Decimal(str(cost_5m)) * Decimal(str(ratio))
        )
        log.info("Auto-filled cache 1hr cost for '%s': %s * %s = %s", key, cost_5m, ratio, value["cache_creation_input_token_cost_above_1hr"])
        count += 1

    return count


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def compute_hash(json_bytes: bytes) -> str:
    """Return hex SHA-256 of the given bytes."""
    return hashlib.sha256(json_bytes).hexdigest()


def write_output(data: dict, json_path: str, hash_path: str, old_hash: str) -> tuple[bool, str]:
    """Write sorted JSON and SHA-256 hash file.

    Returns (changed: bool, new_hash: str).
    """
    json_bytes = (json.dumps(data, sort_keys=True, indent=2) + "\n").encode("utf-8")
    new_hash = compute_hash(json_bytes)

    if new_hash == old_hash:
        log.info("No changes detected (hash matches).")
        return False, new_hash

    with open(json_path, "wb") as f:
        f.write(json_bytes)
    with open(hash_path, "w", encoding="utf-8") as f:
        f.write(new_hash + "\n")

    log.info("Output written: %s (%d models)", json_path, len(data))
    log.info("Hash written:   %s", hash_path)
    return True, new_hash


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync model pricing from upstream.")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--repo-root", default=".", help="Repository root directory")
    args = parser.parse_args()

    repo_root = os.path.abspath(args.repo_root)
    config_path = os.path.join(repo_root, args.config)

    # 1. Load config
    config = load_config(config_path)

    output_path = os.path.join(repo_root, config["output_file"])
    hash_path = os.path.join(repo_root, config["hash_file"])

    # 2. Load existing data
    existing = load_existing(output_path)
    old_hash = load_existing_hash(hash_path)
    log.info("Existing output has %d models.", len(existing))

    # 3. Fetch upstream
    upstream = fetch_upstream(config["upstream_url"])

    # 4. Filter
    filtered = filter_upstream(upstream, config)
    required_fields = config.get("required_price_fields", {})
    required_sources = config.get("required_price_sources", [])
    validate_required_price_fields(filtered, required_fields, required_sources, allow_aliases=True)
    validate_token_price_overrides(
        filtered, config.get("require_token_price_overrides", []), config.get("custom_models", {}),
    )

    # 5. Merge
    merged, stats = merge_models(
        existing,
        filtered,
        config["sync_mode"],
        config.get("update_existing", False),
        tuple(config.get("refresh_existing_prefixes", [])),
    )
    log.info(
        "Merge stats: %d added, %d updated, %d unchanged.",
        stats["added"],
        stats["updated"],
        stats["unchanged"],
    )

    # 6. Restore historical entries without overriding current data.
    fallback_path = config.get("legacy_fallback_file")
    if fallback_path:
        merged = apply_legacy_fallbacks(merged, os.path.join(repo_root, fallback_path))

    # 7. Auto-fill cache 1hr pricing
    cache_1hr_count = fill_cache_1hr_pricing(merged, config)

    # 8. Custom models
    custom = config.get("custom_models", {})
    if custom:
        merged = apply_custom_models(merged, custom)

    # 9. Site-managed entries
    overrides = load_price_overrides(
        os.path.join(repo_root, "price_overrides.json")
    )
    merged = apply_price_overrides(merged, overrides)

    # 10. Aliases must copy the final source entries.
    aliases = config.get("aliases", {})
    merged = apply_aliases(
        merged, aliases, set(overrides), upstream=filtered, custom=custom,
        required_upstream_aliases=config.get("required_upstream_aliases", []),
    )
    validate_required_price_fields(merged, required_fields, required_sources)

    # 11. Write output
    changed, new_hash = write_output(merged, output_path, hash_path, old_hash)

    # 12. Report
    log.info("--- Sync Report ---")
    log.info("Total models in output: %d", len(merged))
    log.info("Added:     %d", stats["added"])
    log.info("Updated:   %d", stats["updated"])
    log.info("Unchanged: %d", stats["unchanged"])
    log.info("Aliases:   %d", len(aliases))
    log.info("Cache 1hr auto-filled: %d", cache_1hr_count)
    log.info("Custom:    %d", len(custom))
    log.info("Overrides: %d", len(overrides))

    # Machine-readable output for CI
    print(f"CHANGED={str(changed).lower()}")
    print(f"HASH={new_hash}")


if __name__ == "__main__":
    main()
