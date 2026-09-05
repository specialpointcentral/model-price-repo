# model-price-repo

Filtered model pricing data for CRS and sub2api projects. Syncs from the upstream [litellm](https://github.com/BerriAI/litellm) pricing file on a schedule, applying configurable prefix filters to keep only the models you actually use.

## How it works

A GitHub Actions workflow runs every 10 minutes (and on manual trigger):

1. Downloads the full `model_prices_and_context_window.json` from litellm
2. Filters models by the prefix rules in `config.json`
3. Merges new models into the existing output (additive — never removes)
4. Auto-fills configured cache prices and applies custom model definitions
5. Replaces complete entries listed in `price_overrides.json`
6. Builds aliases from the final custom or managed source entries
7. Writes the output JSON + SHA-256 hash, committing only if content changed

The transformation order is deliberately fixed:

```text
fetch/filter/merge -> cache auto-fill -> custom models -> managed whole-entry replacement -> aliases -> JSON/SHA-256
```

## Site-managed price cards

Most entries continue to follow litellm automatically. Models listed in
[`price_overrides.json`](price_overrides.json) are different: the complete model
entry is maintained by this repository and replaces the synchronized entry
wholesale. This prevents upstream tier fields from being mixed with locally
approved prices.

The initial managed set is:

- `gpt-5.6-luna`
- `gpt-5.6-terra`

Aliases are generated after managed replacements. In particular,
`codex-auto-review` always copies the final managed `gpt-5.6-luna` entry.

When adding or updating a managed model, review the entire entry, including
standard, priority, flex, batch, cache, long-context, endpoint, capability, and
token-limit fields. A managed key must not also exist in
`config.json.custom_models`.

## Configuration

All settings live in [`config.json`](config.json):

| Field | Description |
|---|---|
| `upstream_url` | URL to the upstream litellm pricing JSON |
| `output_file` | Output filename (default: `model_prices_and_context_window.json`) |
| `hash_file` | SHA-256 hash filename for change detection |
| `sync_mode` | `"additive"` (only add new) or `"full"` (replace each run) |
| `update_existing` | Whether to update pricing data for models already in the output |
| `prefix_filters` | List of prefixes — a model key must start with one to be included |
| `exclude_patterns` | Substring patterns to exclude (applied before prefix matching) |
| `aliases` | Map alias model keys to existing source models (deep copy pricing) |
| `custom_models` | Manually defined pricing objects, always injected |

### Adding new model prefixes

Edit the `prefix_filters` array in `config.json`:

```json
{
  "prefix_filters": [
    "claude-",
    "gpt-",
    "your-new-prefix/"
  ]
}
```

### Adding aliases

Aliases create copies of an existing model's pricing under a new key:

```json
{
  "aliases": {
    "claude-opus-4-6-thinking": {
      "source": "claude-opus-4-6",
      "description": "Thinking variant, same pricing"
    }
  }
}
```

If the source model doesn't exist in the filtered data, the alias is skipped with a warning.

## Running locally

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

To rebuild from a fresh upstream snapshot:

```bash
bash rebuild.sh
```

Then prove that the generated catalog and hash are stable:

```bash
python3 scripts/sync_prices.py --config config.json --repo-root .
```

The final command must print `CHANGED=false`. No pip dependencies are required;
the script and tests use only the Python standard library.

## CRS integration

Point CRS to the raw output file from this repo:

```
MODEL_PRICES_URL=https://raw.githubusercontent.com/<owner>/model-price-repo/main/model_prices_and_context_window.json
```

The output JSON structure is identical to what litellm produces (model key -> pricing object), so CRS `pricingService.js` works without changes.

## License

[MIT](LICENSE)
