"""
Lightweight disk cache for Stage 2 LLM classifications and Stage 3 Anthropic
extractions. Skips repeating API calls when the same PDF (or page) is processed
again with the same model + prompts + schema.

Layout:
    .cache/
      classifications/<hash>.json   ← Stage 2 (Together AI page classifier)
      extractions/<hash>.json       ← Stage 3 (Anthropic batch extraction)

Cache keys are SHA-256 hashes of ALL inputs that affect the output, so any
change to model / prompt / schema / text automatically invalidates the entry.

Usage:
    import cache
    hit = cache.get("classifications", model, page_text)
    if hit is None:
        result = call_api(...)
        cache.set("classifications", result, model, page_text)
    else:
        result = hit

Disable globally with: cache.set_enabled(False)
"""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Optional

CACHE_ROOT = Path(__file__).resolve().parent / ".cache"

_ENABLED = True


def set_enabled(value: bool) -> None:
    """Globally enable/disable the cache (e.g. via --no-cache CLI flag)."""
    global _ENABLED
    _ENABLED = bool(value)


def is_enabled() -> bool:
    return _ENABLED


def _hash(*parts) -> str:
    joined = "\x00".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def _path(namespace: str, key: str) -> Path:
    return CACHE_ROOT / namespace / f"{key}.json"


def get(namespace: str, *parts) -> Optional[dict]:
    """Look up a cache entry. Returns None on miss or when caching disabled."""
    if not _ENABLED:
        return None
    key = _hash(*parts)
    path = _path(namespace, key)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def set(namespace: str, value: dict, *parts) -> None:
    """Store a cache entry. No-op if caching disabled."""
    if not _ENABLED:
        return
    key = _hash(*parts)
    path = _path(namespace, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # Caching failures must never crash the pipeline
        pass


def stats() -> dict:
    """Return {namespace: entry_count} for the on-disk cache."""
    out = {}
    if CACHE_ROOT.exists():
        for ns_dir in CACHE_ROOT.iterdir():
            if ns_dir.is_dir():
                out[ns_dir.name] = sum(1 for _ in ns_dir.glob("*.json"))
    return out


def clear() -> int:
    """Delete all cache entries. Returns number of files removed."""
    if not CACHE_ROOT.exists():
        return 0
    count = sum(1 for _ in CACHE_ROOT.rglob("*.json"))
    shutil.rmtree(CACHE_ROOT)
    return count
