"""
Company Comment (Note) generator
================================

Builds the analyst "Comments" note for a company using:
  1. Firecrawl /search  -> press release / earnings coverage (markdown)
  2. One Claude call    -> composes the note in the fixed 5-step format

Fixed structure (do not change):
  1. Company name -> country -> industry
  2. Latest results: good / bad
  3. Sales (and profits) direction
  4. Management (CEO) comments from press releases — verbatim quote.
     If no quote found: summary of results + highlights of the quarter.
  5. Balance-sheet stats + recommendation close — ONLY when the caller
     supplies pre-computed numbers (net debt %, coverage, FCFF, yield,
     methods, BUY/SELL). Otherwise the note ends after part 4.

All numbers are computed upstream and injected verbatim — the LLM only
does wording, never math. Country/industry may be omitted; the LLM then
infers them from the source material.

Source preference (enforced in _select_sources): official IR site ->
wire release (GlobeNewswire/BusinessWire/PRNewswire) -> sec.gov filing.
Transcript re-posts / analyst articles are used only when nothing better
exists.

Public API:
    generate_comment(client, company, quarter_label=None, country=None,
                     industry=None, financials=None, model=...)
        -> {"comment": str, "sources": [{"type","title","url"}]} | None
"""

from __future__ import annotations

import sys
from typing import Any, Optional
from urllib.parse import urlparse

from core import fc_client

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1024
_MAX_CHARS_PER_SOURCE = 8000   # keep the prompt bounded
_SEARCH_LIMIT = 6              # searched wide, then filtered to the best few
_MAX_SOURCES = 4

# Source preference (lower tier = better). Official IR release first, then a
# wire release, then the SEC-filed release. Transcript re-posts and analyst
# articles are used ONLY when nothing better was found (quotes there are often
# paraphrased / OCR'd from audio).
_WIRE_DOMAINS = ("globenewswire.com", "businesswire.com", "prnewswire.com")
_REJECT_DOMAINS = ("aol.com", "insidermonkey.com", "investing.com",
                   "fool.com", "seekingalpha.com", "247wallst.com")


def _classify(url: str) -> tuple[str, int]:
    """Return (type, tier) for a source URL. Tiers: 1 ir, 2 wire, 3 sec,
    4 other, 9 transcript/analyst (fallback only)."""
    host = (urlparse(url).netloc or "").lower().split(":")[0]

    def under(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    if any(under(d) for d in _REJECT_DOMAINS):
        return "transcript/analyst", 9
    if host.split(".")[0] in ("ir", "investor", "investors"):
        return "ir", 1
    if any(under(d) for d in _WIRE_DOMAINS):
        return "wire", 2
    if under("sec.gov"):
        return "sec", 3
    return "other", 4


def _select_sources(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter + rank search results by source quality. Preferred tiers (ir ->
    wire -> sec) rank first, neutral news pages top up the remaining slots,
    and transcript/analyst pages are admitted only when nothing else usable
    exists at all. A bare homepage (no path — e.g. ir.<company>.com/) rarely
    carries the release text, so it is ranked behind content pages. Sort is
    stable, so search relevance breaks ties within a tier. Returns up to
    _MAX_SOURCES results, each with 'source_type' set."""
    ranked = []
    for res in results:
        url = res.get("url") or ""
        md = (res.get("markdown") or res.get("description") or "").strip()
        if not md:
            continue  # nothing scraped -> nothing to feed the LLM
        typ, tier = _classify(url)
        score = tier + (2.5 if not urlparse(url).path.strip("/") else 0)
        res = dict(res)
        res["source_type"] = typ
        ranked.append((score, res))

    ranked.sort(key=lambda x: x[0])
    usable = [r for s, r in ranked if s < 9]
    if usable:
        return usable[:_MAX_SOURCES]
    # nothing but transcripts/analyst pages found -> allow them as fallback
    return [r for _, r in ranked][:_MAX_SOURCES]

_SAMPLE = (
    "The Kraft Heinz Company, an American food and beverage giant, delivered "
    "good results in the quarter ending in Mar 2026. Sales and profits rose. "
    "Steve Cahillane, CEO of Kraft Heinz, said, “Our first quarter results "
    "demonstrate steady progress, and I am encouraged by the early signs of "
    "momentum we’re building. The investments we made in 2025 are now driving "
    "early traction, with improving market share trends, particularly within "
    "must-win parts of our portfolio like Taste Elevation. This is proof that "
    "our brands respond well when we invest behind them.” The company's "
    "balance sheet shows 23% net debt. Interest coverage stands at 4.8 times. "
    "Free cash flows have been positive in 9 out of 10 years. The company pays "
    "a dividend yielding 6.4%. Overall, the upside from 21 of 24 methods, some "
    "debt, good interest coverage, excellent FCFF trend, good earnings outlook, "
    "and dividend payment make The Kraft Heinz a BUY."
)

_SYSTEM = """You write short company comment notes for an equity analyst, in a professional, polite tone.

You MUST follow this exact 5-part structure, as one flowing paragraph:
1. Open with: company name, its country, and its industry (e.g. "X Company, an American food and beverage giant, ..."). If country or industry are not given in the input, infer them from the source material.
2. State whether the latest quarterly results were good or bad, naming the quarter (e.g. "delivered good results in the quarter ending in Mar 2026").
3. State the direction of sales and profits (e.g. "Sales and profits rose.").
4. Management comment: if the source material contains a management (CEO/CFO) quote about these results, include it VERBATIM in double quotes, attributed by name and title (e.g. 'John Smith, CEO of X, said, "..."'). If NO genuine quote is found in the sources, instead write 2-3 sentences summarising the results and the highlights of the quarter from the sources. NEVER invent, paraphrase-as-quote, or alter a quote.
5. ONLY if a pre-computed data block is provided: close with the balance-sheet facts and recommendation, using those values copied exactly as given — never recompute, round, or change any number. Follow the sample's phrasing: net debt, interest coverage, FCFF years, dividend yield, then the "Overall, ..." sentence ending in the recommendation. If the pre-computed block says "(none provided)", END the note after part 4 — no balance-sheet sentences, no recommendation.

Hard rules:
- Use ONLY facts present in the provided sources and the pre-computed data block.
- Copy every number exactly as given. Do not do any arithmetic.
- Ignore older results; use only material about the latest / stated quarter.
- Output ONLY the finished comment paragraph — no preamble, no headings, no markdown.

Sample of the required style:
""" + _SAMPLE


def _fetch_sources(company: str, quarter_label: Optional[str],
                   limit: int = _SEARCH_LIMIT) -> list[dict[str, Any]]:
    """Search the web for the company's latest results press release. Returns
    Firecrawl search results (title, url, markdown). Empty list on failure so
    the caller can degrade to a sources-free note."""
    period = quarter_label or "latest quarterly"
    query = f"{company} {period} results earnings press release CEO"
    try:
        return fc_client.search(query, limit=limit, scrape_content=True)
    except Exception as e:
        print(f"[comment] search failed for {company!r}: {e}", file=sys.stderr)
        return []


def _sources_block(results: list[dict[str, Any]]) -> str:
    parts = []
    for i, res in enumerate(results, 1):
        md = (res.get("markdown") or res.get("description") or "").strip()
        if not md:
            continue
        typ = res.get("source_type", "other")
        parts.append(
            f"--- SOURCE {i} [{typ}]: {res.get('title', '')} ({res.get('url', '')}) ---\n"
            + md[:_MAX_CHARS_PER_SOURCE]
        )
    return "\n\n".join(parts) if parts else "(no sources found)"


def _financials_block(financials: Optional[dict[str, Any]]) -> str:
    """Render the pre-computed numbers exactly as given (values are used
    verbatim in the note — no math here or in the LLM)."""
    if not financials:
        return "(none provided)"
    lines = [f"- {k}: {v}" for k, v in financials.items() if v is not None]
    return "\n".join(lines) if lines else "(none provided)"


def generate_comment(
    client,
    company: str,
    quarter_label: Optional[str] = None,
    country: Optional[str] = None,
    industry: Optional[str] = None,
    financials: Optional[dict[str, Any]] = None,
    model: str = _MODEL,
    cost_tracker=None,
) -> Optional[str]:
    """Generate the Comments note for one company.

    Args:
        client:        anthropic.Anthropic client instance
        company:       company name, e.g. "The Kraft Heinz Company"
        quarter_label: e.g. "Q1 2026" / "FY2025" (None -> latest quarter)
        country:       e.g. "United States" (None -> inferred from sources)
        industry:      e.g. "food and beverage" (None -> inferred from sources)
        financials:    optional pre-computed values used verbatim, e.g. {
                           "net_debt_pct": "23%",
                           "interest_coverage": "4.8 times",
                           "fcff_positive_years": "9 out of 10 years",
                           "dividend_yield": "6.4%",
                           "methods_upside": "21 of 24 methods",
                           "recommendation": "BUY",
                       }
                       When None/empty the note ends after the management
                       comment — no recommendation is invented.
    Returns {"comment": str, "sources": [{"type", "title", "url"}, ...]}
    (sources = the pages actually fed to the LLM), or None on failure.
    """
    from Anthropic.code import call_claude

    if not company:
        return None

    sources = _select_sources(_fetch_sources(company, quarter_label))

    user_content = (
        f"COMPANY: {company}\n"
        f"COUNTRY: {country or '(infer from sources)'}\n"
        f"INDUSTRY: {industry or '(infer from sources)'}\n"
        f"QUARTER: {quarter_label or '(latest reported — take from sources)'}\n\n"
        f"PRE-COMPUTED DATA (use these values verbatim):\n"
        f"{_financials_block(financials)}\n\n"
        f"SOURCE MATERIAL (press releases / coverage):\n"
        f"{_sources_block(sources)}\n\n"
        f"Write the comment note now."
    )

    try:
        text = call_claude(
            client, _SYSTEM, user_content,
            model=model, max_tokens=_MAX_TOKENS, cost_tracker=cost_tracker,
        )
    except Exception as e:
        print(f"[comment] Claude call failed for {company!r}: {e}", file=sys.stderr)
        return None
    if not text:
        return None
    return {
        "comment": text,
        "sources": [
            {"type": s.get("source_type"),
             "title": s.get("title") or "",
             "url": s.get("url") or ""}
            for s in sources
        ],
    }
