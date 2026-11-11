#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
REBUILD_DIR="$(mktemp -d "$REPO_ROOT/.model-price-rebuild.XXXXXX")"
trap 'rm -rf "$REBUILD_DIR"' EXIT

echo "=== Preparing isolated rebuild ==="
python3 - "$REPO_ROOT" "$REBUILD_DIR" <<'PY'
import json
from pathlib import Path
import shutil
import sys

root, stage = map(Path, sys.argv[1:])
config = json.loads((root / "config.json").read_text(encoding="utf-8"))

def relative(value):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Rebuild path must be relative to the repository: {value}")
    return path

for name in ("config.json", "price_overrides.json", config.get("legacy_fallback_file")):
    if name:
        path = relative(name)
        (stage / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / path, stage / path)
for key in ("output_file", "hash_file"):
    (stage / relative(config[key])).parent.mkdir(parents=True, exist_ok=True)
PY

echo "=== Running sync script ==="
python3 "$REPO_ROOT/scripts/sync_prices.py" --config config.json --repo-root "$REBUILD_DIR"

echo "=== Verifying and installing output ==="
python3 - "$REPO_ROOT" "$REBUILD_DIR" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys

root, stage = map(Path, sys.argv[1:])
config = json.loads((stage / "config.json").read_text(encoding="utf-8"))
output = Path(config["output_file"])
hash_file = Path(config["hash_file"])
actual = hashlib.sha256((stage / output).read_bytes()).hexdigest()
if actual != (stage / hash_file).read_text(encoding="utf-8").strip():
    raise ValueError("Rebuilt catalog does not match its hash")
for path in (output, hash_file):
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage / path, root / path)
PY

echo "=== Done ==="
