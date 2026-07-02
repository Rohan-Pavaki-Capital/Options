"""
Japanese Ticker / Name → EDINET Code Resolver  (library-based)
==============================================================

The Japanese equivalent of dk_resolve.py / uk_resolve.py. EDINET (Japan FSA's
disclosure system) identifies every filer by a 6-char **EDINET code**
(e.g. E02144 for Toyota Motor Corporation). Listed companies also carry a
5-digit **securities code** (the 4-digit TSE ticker plus a trailing 0, e.g.
Toyota 7203 → 72030).

This module bridges "whatever the frontend collected" (a TSE ticker, optionally
a company name) to the authoritative EDINET code using the **`edinet-tools`**
library (https://github.com/matthelmer/edinet-tools). Entity lookup is
performed entirely by the library's bundled EDINET code list and requires **no
API key** — only the document download in `japan_fetch.py` needs one.

Resolution strategy (first hit wins), preserved from the prior implementation:
    1. Numeric ticker (e.g. "7974")  → exact ticker lookup.
    2. Explicit company name          → library name search (handles full/half-
                                         width, gaiji ㈱, middle-dot variants).
    3. Raw ticker string              → name search (last resort).

Public API (unchanged):
    resolve_company_number(ticker, company_name=None)
        -> {company_number, securities_code, title, matched_via, candidates}
"""

from __future__ import annotations

import os
from typing import Any, Optional

# edinet-tools pulls in numpy/pandas; pin OpenBLAS to a single thread before
# that import to avoid the known allocation crash in this venv. Harmless if the
# host already set it (e.g. the backend launches with OPENBLAS_NUM_THREADS=1).
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

_TRUSTSTORE_READY = False


def _ensure_truststore() -> None:
    """Make Python's SSL use the OS (Windows) certificate store.

    edinet-tools calls EDINET via bare urllib with default cert verification and
    no CA-bundle option. On machines behind a TLS-inspecting proxy / AV that
    injects its own root cert, the EDINET handshake otherwise fails with
    CERTIFICATE_VERIFY_FAILED. truststore patches the default SSL context to
    trust whatever the OS already trusts (incl. that root). Verification stays
    ON. Idempotent; only invoked on the Japan path."""
    global _TRUSTSTORE_READY
    if _TRUSTSTORE_READY:
        return
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        # If truststore is unavailable, fall through; on a normal network the
        # default store works and this is a no-op anyway.
        pass
    _TRUSTSTORE_READY = True


# ── Helpers ───────────────────────────────────────────────────────────
def _normalize_sec(ticker_or_code: Optional[str]) -> Optional[str]:
    """Normalize a TSE ticker to the 5-digit securities code.

    "7203" → "72030";  "72030" → "72030";  non-numeric → None.
    """
    digits = "".join(ch for ch in str(ticker_or_code or "") if ch.isdigit())
    if len(digits) == 4:
        return digits + "0"
    if len(digits) == 5:
        return digits
    return None


def _as_result(entity: Any, matched_via: str) -> dict[str, Any]:
    """Map an edinet_tools Entity onto the dict shape backend.py expects."""
    edinet_code = (getattr(entity, "edinet_code", "") or "").strip()
    name_en = (getattr(entity, "name_en", "") or "").strip()
    name_jp = (getattr(entity, "name_jp", "") or "").strip()
    name = (getattr(entity, "name", "") or "").strip()
    title = name_en or name_jp or name or edinet_code

    # The library exposes the 4-digit ticker; reconstruct the 5-digit securities
    # code that callers/UI expect (Toyota 7203 → 72030).
    sec_code = _normalize_sec(getattr(entity, "ticker", None))

    return {
        "company_number": edinet_code,          # EDINET code (used by japan_fetch)
        "securities_code": sec_code,
        "title": title,
        "matched_via": matched_via,
        "candidates": [
            {
                "number": edinet_code,
                "title": name_en or name_jp or name,
                "sec_code": sec_code,
            }
        ],
    }


# ── Public API ────────────────────────────────────────────────────────
def resolve_company_number(
    ticker: str,
    company_name: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a Japanese listing to its EDINET code via `edinet-tools`.

    Returns:
        {
          "company_number": "E02144",   # EDINET code
          "securities_code": "72030",
          "title": "TOYOTA MOTOR CORPORATION",
          "matched_via": "securities_code" | "company_name" | "ticker",
          "candidates": [ {number, title, sec_code}, ... ]
        }

    Raises LookupError if nothing usable is found.
    """
    ticker = (ticker or "").strip().upper()
    company_name = (company_name or "").strip()

    if not ticker and not company_name:
        raise LookupError("No ticker or company name supplied to resolve.")

    # Lazy import: keeps backend startup light and defers the numpy/pandas load
    # until Japan is actually used.
    _ensure_truststore()
    import edinet_tools as et

    entity = None
    matched_via = ""

    # 1) Numeric ticker → exact ticker lookup.
    sec = _normalize_sec(ticker)
    if sec:
        try:
            entity = et.entity_by_ticker(ticker)
        except Exception:
            entity = None
        if entity is not None:
            matched_via = "securities_code"

    # 2) Explicit company name → library name search.
    if entity is None and company_name:
        try:
            entity = et.entity(company_name)
        except Exception:
            entity = None
        if entity is not None:
            matched_via = "company_name"

    # 3) Raw ticker string → name search (last resort).
    if entity is None and ticker:
        try:
            entity = et.entity(ticker)
        except Exception:
            entity = None
        if entity is not None:
            matched_via = "ticker"

    if entity is None or not (getattr(entity, "edinet_code", "") or "").strip():
        raise LookupError(
            f"No EDINET (Japan) company found for {ticker or company_name!r}. "
            f"Try the 4-digit TSE securities code (e.g. 7974) or the registered "
            f"company name."
        )

    return _as_result(entity, matched_via)


if __name__ == "__main__":
    # Manual smoke test:  python jp_resolve.py 7203
    import json
    import sys

    t = sys.argv[1] if len(sys.argv) > 1 else "7203"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(resolve_company_number(t, name), ensure_ascii=False, indent=2))
