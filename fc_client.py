"""
Firecrawl Stealth Client — shared helper for bot-walled markets
================================================================

Several markets (Israel/TASE-MAYA, Singapore/SGX, Malaysia/Bursa,
Thailand/SET, Saudi/Tadawul) front their disclosure portals with hard
anti-bot walls (Akamai / Incapsula / F5) that reject plain HTTP **and**
a headless browser (verified in the 2026-06-03 Asia spike). Firecrawl's
**stealth proxy** (residential IPs + anti-detection) does pass these walls
(verified 2026-06-04), so these fetchers route their *page* requests through
Firecrawl instead of `requests`/Playwright.

This module is a thin wrapper over Firecrawl's REST API (no SDK dependency —
matches the repo's raw-`requests` preference). It needs `FIRECRAWL_API_KEY`
in the environment (the caller — backend/options — runs load_dotenv()).

NOTE on PDFs: Firecrawl *parses* PDFs to markdown; it does not return the
binary the extraction pipeline (PyMuPDF) needs. So the pattern is:
    1. `scrape()` the (walled) listing page -> HTML/markdown/links,
    2. extract the report's PDF URL,
    3. download the binary with `fetch_pdf()` — tries a direct GET first
       (many report CDNs are NOT walled even when the portal is), and only
       falls back to Firecrawl if the direct GET is blocked / non-PDF.

Public API:
    scrape(url, formats=("markdown","links"), wait_ms=4000, ...) -> dict (Firecrawl `data`)
    fetch_pdf(url, referer=None) -> bytes        # raises if no PDF obtainable
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Iterable, Optional

import requests

_SCRAPE_EP = "https://api.firecrawl.dev/v1/scrape"
_CREDIT_EP = "https://api.firecrawl.dev/v1/team/credit-usage"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_DEFAULT_TIMEOUT = 120  # seconds

# Firecrawl credit rates per successful /scrape. A plain scrape is 1 credit;
# the stealth proxy (residential IPs) is billed at 5. Used only to *derive* the
# credit estimate for the TESTING tool — the real ledger is read via
# credit_usage(). Kept here so both rates live in one place.
_CREDITS_STEALTH = 5
_CREDITS_BASIC = 1

# Per-thread scrape accounting. Each background job runs on its own worker
# thread, so a thread-local keeps one job's counts from leaking into another's.
_track = threading.local()


def _key() -> str:
    k = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not k:
        raise RuntimeError(
            "FIRECRAWL_API_KEY not set in environment/.env — required for the "
            "bot-walled markets (Israel/SGX/Bursa/SET/Tadawul)."
        )
    return k


# ── Credit tracking (for the TESTING tool) ─────────────────────────────────
def reset_tracking() -> None:
    """Zero the current thread's scrape counters. Call before a fetch you want
    to measure (e.g. the scrape-test worker)."""
    _track.scrapes = 0
    _track.credits = 0


def record_scrape(stealth: bool = True) -> None:
    """Record one successful Firecrawl scrape on the current thread. Called from
    scrape() and from callers that hit Firecrawl directly (ca_ir_fetch)."""
    _track.scrapes = getattr(_track, "scrapes", 0) + 1
    _track.credits = getattr(_track, "credits", 0) + (
        _CREDITS_STEALTH if stealth else _CREDITS_BASIC
    )


def get_tracking() -> dict[str, int]:
    """Return {scrapes, credits} accumulated on the current thread since the
    last reset_tracking()."""
    return {
        "scrapes": getattr(_track, "scrapes", 0),
        "credits": getattr(_track, "credits", 0),
    }


def credit_usage() -> Optional[int]:
    """Remaining Firecrawl credits for the team, read from the live ledger.
    Returns None if the endpoint is unavailable (no key, plan without the
    endpoint, network error) so callers can degrade gracefully."""
    try:
        r = requests.get(
            _CREDIT_EP,
            headers={"Authorization": "Bearer " + _key()},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        j = r.json()
        data = j.get("data") if isinstance(j.get("data"), dict) else j
        for field in ("remaining_credits", "remainingCredits", "credits"):
            val = data.get(field)
            if isinstance(val, (int, float)):
                return int(val)
    except Exception:
        pass
    return None


def scrape(
    url: str,
    formats: Iterable[str] = ("markdown", "links"),
    wait_ms: int = 4000,
    stealth: bool = True,
    timeout: int = _DEFAULT_TIMEOUT,
    actions: Optional[list[dict[str, Any]]] = None,
    retries: int = 2,
) -> dict[str, Any]:
    """Scrape a (possibly bot-walled) page via Firecrawl. Returns the Firecrawl
    `data` object: {markdown, links, rawHtml, metadata, ...}. Raises on failure.
    """
    body: dict[str, Any] = {
        "url": url,
        "formats": list(formats),
        "waitFor": wait_ms,
        "timeout": timeout * 1000,
        "blockAds": True,
    }
    if stealth:
        body["proxy"] = "stealth"
    if actions:
        body["actions"] = actions

    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                _SCRAPE_EP,
                headers={"Authorization": "Bearer " + _key(),
                         "Content-Type": "application/json"},
                json=body, timeout=timeout + 30,
            )
            j = r.json()
            if r.status_code == 200 and j.get("success") and j.get("data"):
                record_scrape(stealth=stealth)
                return j["data"]
            last_err = f"http={r.status_code} body={str(j)[:200]}"
        except Exception as e:  # network / json error
            last_err = repr(e)
        if attempt < retries:
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"Firecrawl scrape failed for {url!r}: {last_err}")


def fetch_pdf(url: str, referer: Optional[str] = None,
              timeout: int = _DEFAULT_TIMEOUT) -> bytes:
    """Return the binary PDF at `url`. Tries a direct GET first (report CDNs are
    often NOT walled even when the portal is); if that yields a non-PDF / is
    blocked, falls back to Firecrawl. Raises if no PDF can be obtained."""
    headers = {"User-Agent": _UA, "Accept": "application/pdf,*/*"}
    if referer:
        headers["Referer"] = referer
    # 0) curl_cffi with a real Chrome TLS/JA3 fingerprint. Many report hosts
    #    (Akamai/Cloudflare/Imperva — e.g. Manulife, Maybank) block plain requests
    #    at the TLS layer regardless of User-Agent; impersonating Chrome's handshake
    #    gets past that and returns the raw PDF bytes the pipeline needs.
    try:
        from curl_cffi import requests as _creq
        h = {"Referer": referer} if referer else None
        cr = _creq.get(url, impersonate="chrome", timeout=timeout,
                       headers=h, allow_redirects=True)
        if cr.status_code == 200 and cr.content[:4] == b"%PDF":
            return cr.content
    except Exception:
        pass
    # 1) plain direct GET (kept for hosts that don't need impersonation)
    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return r.content
    except Exception:
        pass
    # 2) Firecrawl stealth fetch of the same URL (handles walled CDNs). Firecrawl
    #    parses PDFs, but the metadata may expose the resolved binary; as a last
    #    resort we re-request through the stealth proxy via a fresh session.
    #    (Most markets won't need this; kept as a guard.)
    try:
        data = scrape(url, formats=("rawHtml",), wait_ms=2000)
        # If Firecrawl fetched a PDF it returns parsed text, not bytes — so we
        # cannot reconstruct the binary here. Surface a clear error instead.
        raise RuntimeError(
            "PDF host appears bot-walled; Firecrawl returns parsed text, not the "
            "binary the pipeline needs. Direct download is required for this URL."
        )
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Could not download PDF from {url!r}: {e}")
