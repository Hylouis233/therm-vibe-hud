import json
import time
import urllib.request
from pathlib import Path

# Same public pricing catalog CodexBar itself uses (ModelsDevPricing.swift) —
# published per-model $/M-token rates, not this account's actual invoice.
# Treated as an estimate, same as CodexBar's own cost feature.
MODELS_DEV_URL = "https://models.dev/api.json"
CACHE_PATH = Path(__file__).resolve().parent.parent / "pricing_cache.json"
CACHE_TTL_SEC = 24 * 3600
FETCH_TIMEOUT_SEC = 10

# Prefer each model's canonical/first-party provider entry over resold
# gateway markups (abacus, aihubmix, etc. all list the same models at
# different margins) — canonical numbers are what CodexBar itself surfaces.
CANONICAL_PROVIDER = {
    "claude": "anthropic",
    "gpt": "openai",
}

_cache = None


def _canonical_provider_for(model_id):
    for prefix, provider in CANONICAL_PROVIDER.items():
        if model_id.startswith(prefix):
            return provider
    return None


def _fetch_live():
    req = urllib.request.Request(MODELS_DEV_URL, headers={"User-Agent": "therm-vibe-hud"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
        return json.loads(resp.read())


def _extract(data, model_ids):
    out = {}
    for model_id in model_ids:
        provider = _canonical_provider_for(model_id)
        cost = None
        if provider:
            cost = ((data.get(provider) or {}).get("models") or {}).get(model_id, {}).get("cost")
        if not cost:
            # fall back to whichever provider lists it, if no canonical match
            for pdata in data.values():
                m = (pdata.get("models") or {}).get(model_id)
                if m and m.get("cost"):
                    cost = m["cost"]
                    break
        if cost:
            out[model_id] = cost
    return out


def _load_cache():
    global _cache
    if _cache is not None:
        return _cache
    try:
        _cache = json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        _cache = {"fetched_at": 0, "prices": {}}
    return _cache


def _save_cache():
    try:
        CACHE_PATH.write_text(json.dumps(_cache))
    except OSError:
        pass


def refresh(model_ids):
    """Fetch+cache pricing for the given model IDs if the cache is stale or
    missing any of them. Network failure silently keeps whatever's cached —
    a HUD shouldn't block or blank out over a pricing lookup."""
    cache = _load_cache()
    known = cache.get("prices", {})
    stale = time.time() - cache.get("fetched_at", 0) > CACHE_TTL_SEC
    missing = [m for m in model_ids if m not in known]
    if not stale and not missing:
        return
    try:
        data = _fetch_live()
    except Exception:
        return
    known.update(_extract(data, model_ids))
    cache["prices"] = known
    cache["fetched_at"] = time.time()
    _save_cache()


def price_for(model_id):
    """{'input': $/M, 'output': $/M, 'cache_read': $/M, 'cache_write': $/M} or None."""
    if not model_id:
        return None
    return _load_cache().get("prices", {}).get(model_id)


def estimate_cost_usd(model_id, input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0):
    price = price_for(model_id)
    if not price:
        return None
    m = 1_000_000
    return (
        input_tokens / m * price.get("input", 0)
        + output_tokens / m * price.get("output", 0)
        + cache_read_tokens / m * price.get("cache_read", 0)
        + cache_write_tokens / m * price.get("cache_write", 0)
    )


if __name__ == "__main__":
    import sys

    ids = sys.argv[1:] or ["claude-sonnet-5", "gpt-5.6-sol"]
    refresh(ids)
    for i in ids:
        print(i, price_for(i))
