# model-price-repo

Filtered model pricing data for CRS and sub2api projects. Syncs from the upstream [litellm](https://github.com/BerriAI/litellm) pricing file on a schedule, applying configurable prefix filters to keep only the models you actually use.

## How it works

A GitHub Actions workflow runs every 10 minutes (and on manual trigger):

1. Downloads the full `model_prices_and_context_window.json` from litellm
2. Filters models by the prefix rules in `config.json`
3. Merges models into the existing output, preserving entries and refreshing selected sources
4. Restores missing historical Gemini entries and fields from `legacy_gemini_prices.json`
5. Auto-fills configured cache prices and applies custom model definitions
6. Replaces complete entries listed in `price_overrides.json`
7. Materializes upstream and configured aliases from their final source entries
8. Writes the output JSON + SHA-256 hash, committing only if content changed

The transformation order is deliberately fixed:

```text
fetch/filter/merge -> historical fallbacks -> cache auto-fill -> custom models -> managed whole-entry replacement -> aliases -> JSON/SHA-256
```

Gemini 4's LiteLLM entries use provider-prefixed names. The catalog keeps the
`gemini/` and `vertex_ai/` Gemini 4 entries and also publishes unprefixed names
for Argon, Carbon, their low/medium/high thinking levels, and their Cyber
Permissive variants. The unprefixed ordinary models copy Gemini API data; the
Cyber variants copy Vertex AI data. Those provider-prefixed sources and
`claude-fable-5-2` are refreshed on each sync, and upstream `alias_of` entries
are expanded into priced entries. Selected upstream cards must contain positive
input, output and cache prices before they can replace existing prices.

The ordinary Gemini 4 Argon and Carbon entries use Google's non-promotional
global token rates: $4 input, $20 output, and $0.20 cached input per million
tokens. [Google's promotional rates](https://cloud.google.com/model-garden/pricing)
apply through November 30, 2026; this catalog deliberately uses the standard
rates now. `config.json.custom_models` pins the token price fields on both the
Gemini API and Vertex AI sources, including their existing batch, priority,
reasoning, audio, and flex fields where applicable. Batch and priority use
Google's published non-promotional rates; existing flex fields use the
half-standard rates published for the Gemini API. Vertex AI Carbon currently
has no flex price fields in LiteLLM. If upstream adds an unreviewed token price
field to any of these four source entries, the sync fails before writing so its
rate can be reviewed instead of silently mixing price policies. The unprefixed
and thinking-level aliases inherit the pinned prices, while metadata continues
to refresh. Cyber Permissive already uses the standard rates and follows its
Vertex AI source. The pinned values are absolute, so a future upstream change
from promotional to standard rates will not double them; update the configuration
if Google changes the standard prices.

`legacy_gemini_prices.json` contains historical entries from this repository's
pre-rebase catalog that LiteLLM no longer supplies. These models are marked
deprecated in those records. Fallback data fills missing models or fields only;
current catalog values are not overwritten. Historical prices do not establish
that a retired model is still offered.

## Site-managed price cards

Most entries continue to follow litellm automatically. Models listed in
[`price_overrides.json`](price_overrides.json) are different: the complete model
entry is maintained by this repository and replaces the synchronized entry
wholesale. This prevents upstream tier fields from being mixed with locally
approved prices.

The managed set is:

- `gpt-5.6-luna`
- `gpt-5.6-terra`
- `gpt-6-luna`
- `gpt-6-sol`
- `gpt-6.1-sol`
- `gpt-6.1-terra`
- `gpt-6-astra`

Aliases are generated after managed replacements. In particular,
`codex-auto-review` always copies the final managed `gpt-5.6-luna` entry.

When adding or updating a managed model, review the entire entry, including
standard, priority, flex, batch, cache, long-context, endpoint, capability, and
token-limit fields. A managed key must not also exist in
`config.json.custom_models`.

## GPT-6 site pricing policy

The five GPT-6 / GPT-6.1 managed cards use the Standard token credit ratios in
[ChatGPT pricing](https://learn.chatgpt.com/docs/pricing), applied separately to
the unchanged `gpt-5.6-sol` catalog rates: $5 input, $0.50 cached input and $30
output per million tokens. These are site-specific billing prices, not official
OpenAI API USD prices or a uniform credits-to-USD conversion.

| Model | Input / 1M | Cached input / 1M | Output / 1M |
| --- | ---: | ---: | ---: |
| `gpt-6-astra` | $12.50 | $1.25 | $75 |
| `gpt-6.1-sol` | $2.50 | $0.125 | $15 |
| `gpt-6.1-terra` | $1.25 | $0.0625 | $7.50 |
| `gpt-6-sol` | $2.50 | $0.25 | $15 |
| `gpt-6-luna` | $0.125 | $0.0125 | $0.75 |

- **Fast:** `_priority` prices are **2.5x** the corresponding Standard prices,
  following included subscription usage. Do not substitute the purchased-credit
  / Enterprise pay-as-you-go multiplier of 2x. Terra's preserved cache-write
  prices are an exception to this adjustment (see below).
- **Ultrafast:** `_ultrafast` prices are **8x** Standard for `gpt-6-astra` and
  `gpt-6.1-sol`, where supported, following included subscription usage. The
  purchased-credit multiplier is 6x. See [Speed](https://learn.chatgpt.com/docs/agent-configuration/speed).
- **Long context:** retain the existing site policy above 272,000 input tokens.
  `gpt-6-sol`, `gpt-6.1-sol`, `gpt-6.1-terra` and `gpt-6-luna` use 2x input / cached-input prices
  and 1.5x output prices. Astra has no long-context surcharge. Apply the speed
  multiplier to the matching long-context prices as well. The cited ChatGPT
  pricing page does **not** specify a separate long-context credit rate; these
  multipliers are retained site policy, not a newly verified official credit rule.
- **Cache writes:** Astra, Sol, 6.1 Sol and Luna have zero separate cache-write surcharge,
  consistent with the ChatGPT credit documentation. Ordinary uncached input
  remains billable. By explicit site policy, Terra preserves all 16 existing
  upstream cache-write prices from `087f5b8`, including Fast, long-context and
  above-12hr retention tiers, without rescaling. Its cache-write Fast multiplier
  therefore remains 2x. Other models, including the GPT-5.6 baseline, are unchanged.
- Existing Flex / Batch discounts remain 0.5x the corresponding Standard tier
  as a site policy; the credit page does not establish those discounts.
- `gpt-6.1-sol` and `gpt-6.1-terra` are complete managed replacements, so additive upstream syncs
  cannot restore stale or mixed-tier pricing.

## Configuration

All settings live in [`config.json`](config.json):

| Field | Description |
|---|---|
| `upstream_url` | URL to the upstream litellm pricing JSON |
| `output_file` | Output filename (default: `model_prices_and_context_window.json`) |
| `hash_file` | SHA-256 hash filename for change detection |
| `sync_mode` | `"additive"` (only add new) or `"full"` (replace each run) |
| `update_existing` | Whether to update pricing data for models already in the output |
| `refresh_existing_prefixes` | Prefixes whose existing entries are replaced with current upstream data on each sync |
| `required_price_sources` | Upstream model names that must be present before syncing |
| `required_upstream_aliases` | Upstream aliases that must be present and resolve to a price card |
| `required_price_fields` | Required positive price fields by prefix; an incomplete upstream entry fails the sync |
| `legacy_fallback_file` | Historical model entries used only to fill missing models or fields |
| `prefix_filters` | List of prefixes — a model key must start with one to be included |
| `exclude_patterns` | Substring patterns to exclude (applied before prefix matching) |
| `aliases` | Map alias model keys to existing source models (deep copy pricing) |
| `custom_models` | Manually defined pricing objects, always injected |
| `require_token_price_overrides` | Source models whose upstream token-price fields must all have local custom overrides |

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
