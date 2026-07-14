"""
credit_route.py — standalone "company credit rating" feature.

Exposes an APIRouter that app.py mounts on the SAME origin / uvicorn server as
the Simply Wall St forecast and the beta lookup:

  GET  /api/credit-rating?company=HP   -> company -> credit rating (JSON)

What it does (matches the workflow in the screenshots):
  1. Fetch the AI-recommended credit rating the way a Google/Bing search shows
     it — e.g. searching "HP S&P Global company rating" surfaces "BBB". We pull
     that text through Firecrawl's residential-IP proxy (same approach as the
     beta feature) and parse the agency rating out of it.
  2. Map the fetched rating onto the analyst's dropdown scale (the Moody's/S&P
     pairs in the image: A1/A+, A2/A, ... Baa2/BBB, ... D2/D). Any notch the
     dropdown doesn't list (BBB+, AA, BB-, ...) is SNAPPED to the nearest listed
     value, preferring the same letter family. So BBB+/BBB/BBB- all -> Baa2/BBB.
  3. Return JSON with the company name and the mapped dropdown rating.

Output shape:
  {
    "company": "HP",
    "rating": "Baa2/BBB",          # value from the dropdown scale
    "fetched_rating": "BBB",        # raw rating as found in the search result
    "agency": "S&P",                # agency the raw rating came from (best effort)
    "outlook": "Stable",            # outlook if stated, else null
    "source": "firecrawl"
  }

Datacenter-IP note (mirrors Simply_wlst/data.py and beta_route.py):
  Search engines block cloud/datacenter IPs, so the fetch goes through
  Firecrawl when FIRECRAWL_API_KEY is set. There is no useful no-key fallback
  for a search-engine scrape, so the key is required for this feature.
"""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

# Route the search-engine fetch through Firecrawl (residential IP) so it isn't
# blocked. Required for this feature — a bare datacenter request gets walled.
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()


# ---------------------------------------------------------------------------
# Rating scale
# ---------------------------------------------------------------------------
# Canonical full rating ladder, best (rank 1) -> worst (rank 22), pairing each
# S&P notch with its Moody's equivalent. Used to give every rating a numeric
# rank so we can snap to the nearest dropdown value.
CANON = [
    (1,  "AAA",  "Aaa"),
    (2,  "AA+",  "Aa1"),
    (3,  "AA",   "Aa2"),
    (4,  "AA-",  "Aa3"),
    (5,  "A+",   "A1"),
    (6,  "A",    "A2"),
    (7,  "A-",   "A3"),
    (8,  "BBB+", "Baa1"),
    (9,  "BBB",  "Baa2"),
    (10, "BBB-", "Baa3"),
    (11, "BB+",  "Ba1"),
    (12, "BB",   "Ba2"),
    (13, "BB-",  "Ba3"),
    (14, "B+",   "B1"),
    (15, "B",    "B2"),
    (16, "B-",   "B3"),
    (17, "CCC+", "Caa1"),
    (18, "CCC",  "Caa2"),
    (19, "CCC-", "Caa3"),
    (20, "CC",   "Ca"),
    (21, "C",    "C"),
    (22, "D",    "D"),
]

# The analyst's dropdown values, exactly as they appear in the image, each
# tagged with the S&P notch it represents (used to look up its rank).
DROPDOWN = [
    ("A1/A+",    "A+"),
    ("A2/A",     "A"),
    ("A3/A-",    "A-"),
    ("Baa2/BBB", "BBB"),
    ("Ba1/BB+",  "BB+"),
    ("Ba2/BB",   "BB"),
    ("B1/B+",    "B+"),
    ("B2/B",     "B"),
    ("B3/B-",    "B-"),
    ("Caa/CCC",  "CCC"),
    ("Ca2/CC",   "CC"),
    ("C2/C",     "C"),
    ("D2/D",     "D"),
]

# token (UPPER) -> rank, and rank -> S&P notch, built from CANON. Tokens come
# from both agencies; C and D coincide (same rank), nothing else collides.
_TOKEN_RANK: dict[str, int] = {}
_RANK_SP: dict[int, str] = {}
for _rank, _sp, _moody in CANON:
    _TOKEN_RANK[_sp.upper()] = _rank
    _TOKEN_RANK[_moody.upper()] = _rank
    _RANK_SP[_rank] = _sp

# dropdown label -> rank (via its S&P notch).
_DROPDOWN_RANK = [(label, _TOKEN_RANK[sp.upper()], sp) for label, sp in DROPDOWN]

# Every recognised rating token, longest first so "BBB+" is matched before
# "BBB" and "Baa1" before any shorter prefix. '+'/'-' are part of the token.
_ALL_TOKENS = sorted(
    {t for _, sp, m in CANON for t in (sp, m)},
    key=len,
    reverse=True,
)
_RATING_RE = re.compile(
    r"(?<![A-Za-z0-9.+\-])(?:" + "|".join(re.escape(t) for t in _ALL_TOKENS) + r")(?![A-Za-z0-9])"
)

# Words that signal we're looking at a real rating mention, not a stray capital
# letter. Used to score candidate matches by proximity.
_CONTEXT_RE = re.compile(
    r"rating|credit|S&P|S&amp;P|Standard\s*&|Moody|Fitch|Global Ratings|long[- ]term|outlook|investment grade",
    re.I,
)
_OUTLOOK_RE = re.compile(r"\b(stable|positive|negative|developing)\b\s+outlook|outlook[:\s]+\b(stable|positive|negative|developing)\b", re.I)


def _family(sp: str) -> str:
    """Letter family of an S&P notch: 'BBB+' -> 'BBB', 'A-' -> 'A'."""
    return re.sub(r"[+\-]$", "", sp)


def _agency_for_token(token: str) -> str:
    """Which agency's scale a token belongs to (best effort).

    Matched case-sensitively: S&P notches are all-caps (AAA, BBB) while Moody's
    are mixed-case (Aaa, Baa2), so 'Aaa' resolves to Moody's even though it
    upper-cases to S&P's 'AAA'. Bare 'C'/'D' coincide on both scales.
    """
    moody = {m for _, _, m in CANON}
    sp = {s for _, s, _ in CANON}
    in_moody, in_sp = token in moody, token in sp
    if in_moody and not in_sp:
        return "Moody's"
    if in_sp and not in_moody:
        return "S&P"
    return "S&P/Moody's"  # C and D coincide


def map_rating(token: str) -> str | None:
    """Snap a fetched rating token to the nearest dropdown value.

    Ties prefer the same S&P letter family, then the better (higher) rating.
    Returns the dropdown label (e.g. 'Baa2/BBB') or None if the token is not a
    recognised rating.
    """
    rank = _TOKEN_RANK.get(token.upper())
    if rank is None:
        return None
    fam = _family(_RANK_SP[rank])
    label, _, _ = min(
        _DROPDOWN_RANK,
        key=lambda d: (abs(d[1] - rank), 0 if _family(d[2]) == fam else 1, d[1]),
    )
    return label


def parse_rating(text: str):
    """Extract the most likely agency rating from search-result text.

    Scores each recognised rating token by proximity to rating-context words
    and by length (single letters like 'A' are risky), then returns the best.
    Returns (token, agency, outlook) or None.
    """
    if not text:
        return None

    ctx_spans = [m.start() for m in _CONTEXT_RE.finditer(text)]

    best = None  # (score, -position, token)
    for m in _RATING_RE.finditer(text):
        token = m.group(0)
        pos = m.start()
        near = min((abs(pos - c) for c in ctx_spans), default=10 ** 9)
        score = 0
        if len(token) >= 2:
            score += 2
        if near <= 60:
            score += 3
        elif near <= 160:
            score += 1
        # A bare single letter (A/B/C/D) with no nearby context is almost
        # certainly a false positive — skip it.
        if len(token) == 1 and near > 60:
            continue
        cand = (score, -pos, token)
        if best is None or cand > best:
            best = cand

    if best is None:
        return None

    token = best[2]
    outlook_m = _OUTLOOK_RE.search(text)
    outlook = None
    if outlook_m:
        outlook = (outlook_m.group(1) or outlook_m.group(2) or "").title() or None
    return token, _agency_for_token(token), outlook


# ---------------------------------------------------------------------------
# Firecrawl fetch
# ---------------------------------------------------------------------------
def _firecrawl_scrape_search(company: str) -> str:
    """Scrape the Google results page for the company's rating, return markdown.

    The query mirrors the screenshot ("HP S&P Global company rating"); the
    results page text carries the AI-recommended rating plus the supporting
    snippets we parse.
    """
    import requests

    query = f"{company} S&P Global Moody's credit rating"
    url = "https://www.google.com/search?q=" + requests.utils.quote(query)
    r = requests.post(
        "https://api.firecrawl.dev/v1/scrape",
        headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                 "Content-Type": "application/json"},
        json={"url": url, "formats": ["markdown"], "location": {"country": "US"}},
        timeout=120,
    )
    try:
        d = r.json()
    except Exception:
        raise HTTPException(status_code=502,
                            detail=f"Firecrawl returned non-JSON (HTTP {r.status_code}).")
    if not d.get("success"):
        raise HTTPException(status_code=502,
                            detail=f"Firecrawl error (HTTP {r.status_code}): "
                                   f"{d.get('error') or d.get('details') or 'unknown'}")
    return (d.get("data") or {}).get("markdown", "") or ""


def _firecrawl_search(company: str) -> str:
    """Fallback: Firecrawl /search, return the joined result snippets+content."""
    import requests

    query = f"{company} credit rating S&P Moody's"
    r = requests.post(
        "https://api.firecrawl.dev/v1/search",
        headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                 "Content-Type": "application/json"},
        json={"query": query, "limit": 6},
        timeout=120,
    )
    try:
        d = r.json()
    except Exception:
        return ""
    parts = []
    for item in (d.get("data") or []):
        if isinstance(item, dict):
            parts.append(item.get("title", "") or "")
            parts.append(item.get("description", "") or "")
            parts.append(item.get("markdown", "") or "")
    return "\n".join(parts)


def get_rating(company: str) -> dict:
    """Resolve a company name to a mapped dropdown credit rating."""
    if not FIRECRAWL_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="FIRECRAWL_API_KEY is not set; the credit-rating lookup needs it.",
        )

    text = _firecrawl_scrape_search(company)
    parsed = parse_rating(text)
    if parsed is None:
        # AI overview / featured snippet didn't surface a rating — try /search.
        parsed = parse_rating(_firecrawl_search(company))
    if parsed is None:
        raise HTTPException(
            status_code=404,
            detail=f"Could not find a credit rating for '{company}'.",
        )

    token, agency, outlook = parsed
    mapped = map_rating(token)
    return {
        "company": company,
        "rating": mapped,
        "fetched_rating": token,
        "agency": agency,
        "outlook": outlook,
        "source": "firecrawl",
    }


@router.get("/api/credit-rating")
def api_credit_rating(
    company: str = Query(..., description="Company name, e.g. HP"),
):
    """Company name -> AI-recommended credit rating, mapped to the scale (JSON)."""
    company = (company or "").strip()
    if not company:
        raise HTTPException(status_code=400, detail="A company name is required.")
    return get_rating(company)


# ---------------------------------------------------------------------------
# Standalone CLI for quick testing:  python credit_route.py "HP"
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys

    # Load FIRECRAWL_API_KEY from a sibling/parent .env when run directly.
    if not FIRECRAWL_API_KEY:
        for _dir in (os.path.dirname(__file__), os.path.dirname(os.path.dirname(__file__))):
            _envp = os.path.join(_dir, ".env")
            if os.path.exists(_envp):
                with open(_envp, encoding="utf-8") as _f:
                    for _line in _f:
                        if _line.strip().startswith("FIRECRAWL_API_KEY"):
                            FIRECRAWL_API_KEY = _line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
            if FIRECRAWL_API_KEY:
                break

    name = " ".join(sys.argv[1:]) or "HP"
    try:
        print(json.dumps(get_rating(name), indent=2, ensure_ascii=False))
    except HTTPException as e:
        print(f"ERROR {e.status_code}: {e.detail}")
