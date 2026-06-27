"""
Canada (TSX-only) Investor-Relations Filing Fetcher
===================================================

For Canadian issuers that are NOT SEC-registered (so `ca_fetch`/EDGAR can't
reach them) and that SEDAR+ won't serve to a bot, this fetcher finds the
issuer's most recent **annual report / audited financial statements PDF** the
way a person would: search the web, land on the company's own investor-relations
site, and download the PDF.

Design goals (per request):
  * **DuckDuckGo for search** (free, no API key) via the `ddgs` client.
  * **Spend as little Firecrawl as possible** — plain `requests` does the work;
    Firecrawl is used ONLY as a last resort to read an IR page that blocks a
    plain HTTP GET (and even then only to harvest PDF links — the PDF binary is
    still downloaded over plain HTTP). PDFs themselves are rarely bot-walled.

Strategy:
  1. DuckDuckGo search for the issuer's annual report / financial statements.
  2. Prefer a PDF hosted on the issuer's OWN domain (annual report / audited
     financial statements, newest year, not an interim Q1-Q3 / proxy / deck).
  3. If no good direct PDF, open the best issuer IR page and harvest the most
     recent annual-report PDF link from it.
  4. Download → OCR defensively → hand to the pipeline.

Public API:
    fetch_filing_as_pdf(company_number, category, out_pdf_path, company_name, ...) -> dict
"""

from __future__ import annotations

import datetime
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

import requests

import fc_client
import ocr_pdf

_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
}
_TIMEOUT = 60

# Corporate suffixes / filler dropped when deriving domain-match tokens.
# NOTE: do NOT drop geographic brand words like "canadian"/"canada" — for names
# such as "Canadian Tire" they are the distinctive part (the generic "tire"
# alone falsely matched bkt-tires.com).
_STOPWORDS = {
    "inc", "incorporated", "corp", "corporation", "ltd", "limited", "company",
    "co", "plc", "the", "and", "group", "holdings", "holding", "international",
    "tsx", "common", "shares", "class",
}
# Words that mark a NON-annual document we should avoid.
_BAD = ("proxy", "circular", "mda", "md&a", "management discussion",
        "presentation", "webcast", "transcript", "factsheet", "fact-sheet",
        "fact_sheet", "sustainability", "esg", "agm", "notice", "prospectus",
        "supplement", "press", "news", "release")


def _name_tokens(name: str) -> list[str]:
    """Significant lowercase tokens of a company name, for domain matching."""
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    return [t for t in toks if len(t) >= 4 and t not in _STOPWORDS]


def _slug(tokens: list[str]) -> str:
    """Concatenated name slug, e.g. ['canadian','tire'] -> 'canadiantire'."""
    return "".join(tokens)


def _host_matches(host: str, tokens: list[str]) -> bool:
    """True only on a STRONG issuer-domain match: the full name slug, or a
    distinctive (len>=5) token. Generic short tokens like 'tire' don't count,
    so a different tire company (bkt-tires.com) won't false-match Canadian Tire."""
    host = (host or "").lower()
    slug = _slug(tokens)
    if len(slug) >= 6 and slug in host.replace("-", "").replace("_", ""):
        return True
    return any(len(t) >= 5 and t in host for t in tokens)


def _pdf_is_company(data: bytes, tokens: list[str]) -> bool:
    """Verification guard: confirm the downloaded PDF actually belongs to this
    issuer by checking its front matter for the company name. Prevents
    wrong-company downloads (e.g. a same-keyword foreign company)."""
    if not tokens:
        return True
    try:
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            head = " ".join(doc.load_page(i).get_text()
                             for i in range(min(4, len(doc)))).lower()
    except Exception:
        return True   # can't verify → don't block (OCR/odd PDFs)
    distinctive = [t for t in tokens if len(t) >= 5]
    # Require the slug, OR every distinctive token, OR (if none long) all tokens.
    if _slug(tokens) in head.replace(" ", ""):
        return True
    need = distinctive or tokens
    return all(t in head for t in need)


def _min_year() -> int:
    """Oldest acceptable report year (current year minus 3)."""
    return datetime.date.today().year - 3


def _doc_year(data: bytes) -> Optional[int]:
    """Apparent report year = newest plausible year in the PDF's front matter."""
    try:
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            head = " ".join(doc.load_page(i).get_text()
                            for i in range(min(3, len(doc))))
    except Exception:
        return None
    cur = datetime.date.today().year
    yrs = [int(y) for y in re.findall(r"20\d\d", head) if 2010 <= int(y) <= cur + 1]
    return max(yrs) if yrs else None


def _recent_enough(data: bytes) -> bool:
    """Reject very old archived reports (project rule: avoid very old data).
    If the year can't be determined, don't block."""
    y = _doc_year(data)
    return y is None or y >= _min_year()


def _ddg(query: str, max_results: int = 12) -> list[dict[str, str]]:
    """DuckDuckGo text search via the ddgs client. Returns [{title, href}]."""
    try:
        from ddgs import DDGS
    except Exception:
        return []
    try:
        with DDGS() as d:
            return [{"title": r.get("title", ""), "href": r.get("href", "")}
                    for r in d.text(query, max_results=max_results) if r.get("href")]
    except Exception:
        return []


def _years(blob: str) -> list[int]:
    """All plausible report years mentioned: full (2025) and FY two-digit (FY25)."""
    yrs = [int(y) for y in re.findall(r"20\d\d", blob)]
    yrs += [2000 + int(y) for y in re.findall(r"fy\s*['\-]?\s*(\d{2})", blob)]
    return [y for y in yrs if 2010 <= y <= 2031]


def _score_pdf(url: str, title: str, tokens: list[str]) -> int:
    """Rank a candidate PDF URL — higher = more likely the latest annual report
    / audited financial statements on the issuer's own site."""
    u = url.lower()
    if ".pdf" not in u:
        return -1000
    blob = u + " " + (title or "").lower()
    host = urlparse(u).netloc.lower()
    score = 0

    if tokens and _host_matches(host, tokens):
        score += 60                       # issuer's own domain — strongest signal
    if "annual" in blob and "report" in blob:
        score += 40
    if ("financial" in blob and "statement" in blob) or re.search(r"[-_/]fs[-_.]", u):
        score += 38                       # audited financial statements (where the note lives)
    if re.search(r"[-_/]ar[-_.]|annual", u):
        score += 12

    yrs = _years(blob)
    if yrs:
        score += (max(yrs) - 2015) * 4    # newer = better

    # Interim quarters (Q1-Q3) and other non-annual docs are wrong.
    if re.search(r"\bq[123]\b|[-_/]q[123][-_/]|quarter|interim", blob):
        score -= 90
    for bad in _BAD:
        if bad in blob:
            score -= 70
    # aggregator domains are OK but worse than the issuer's own site
    if "annualreports.com" in host:
        score += 20
    return score


def _download_pdf(url: str) -> Optional[bytes]:
    try:
        r = requests.get(url, headers=_UA, timeout=_TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return r.content
    except Exception:
        pass
    return None


def _page_pdf_links(page_url: str) -> list[tuple[str, str]]:
    """Fetch an IR page and return [(abs_pdf_url, anchor_text)]. Plain HTTP first;
    Firecrawl ONLY if the page blocks us (minimise Firecrawl spend)."""
    html = ""
    try:
        r = requests.get(page_url, headers=_UA, timeout=_TIMEOUT)
        if r.status_code == 200 and "<html" in r.text.lower():
            html = r.text
    except Exception:
        html = ""

    if not html:                          # blocked / JS-only → last-resort Firecrawl
        html = _firecrawl_html(page_url)
    if not html:
        return []

    out = []
    for m in re.finditer(r'href=["\']([^"\']+\.pdf[^"\']*)["\']([^>]*)>([^<]*)', html, re.I):
        href = urljoin(page_url, m.group(1))
        text = (m.group(3) or "").strip()
        out.append((href, text))
    return out


def _firecrawl_html(url: str) -> str:
    """Last-resort: read a bot-walled IR page via Firecrawl (links only — the PDF
    is still fetched over plain HTTP). No-op if no key is configured."""
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        return ""
    try:
        r = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"url": url, "formats": ["html"], "proxy": "stealth", "onlyMainContent": False},
            timeout=120,
        )
        if r.status_code == 200:
            fc_client.record_scrape(stealth=True)  # count toward TESTING credit metrics
            return (r.json().get("data") or {}).get("html") or ""
    except Exception:
        pass
    return ""


def fetch_filing_as_pdf(
    company_number: str = "",
    category: str = "annual",
    out_pdf_path: str | Path = "filing.pdf",
    company_name: Optional[str] = None,
    ocr_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Find & download a TSX-only issuer's latest annual report / financial
    statements PDF via web search + its IR site. Signature mirrors the other
    fetchers. `company_name` is strongly recommended (ticker alone is weak)."""
    ticker = str(company_number or "").strip()
    name = (company_name or "").strip()
    if not name and not ticker:
        raise ValueError("company_name (preferred) or ticker is required.")

    tokens = _name_tokens(name) or _name_tokens(ticker)
    label = name or ticker

    # 1) Search for the document directly — bias toward the most recent year.
    yr = datetime.date.today().year
    queries = []
    if name:
        queries.append(f"{name} {yr - 1} annual report financial statements filetype:pdf")
        queries.append(f"{name} annual report audited financial statements filetype:pdf")
        queries.append(f"{name} investor relations financial reports {yr}")
    else:
        queries.append(f"{ticker} TSX {yr - 1} annual report filetype:pdf")

    results: list[dict[str, str]] = []
    for q in queries:
        results += _ddg(q, max_results=12)
        time.sleep(0.5)
    if not results:
        raise LookupError(
            f"Web search returned nothing for {label!r}. Try the full registered "
            f"company name, or upload the PDF manually."
        )

    # 2) Best direct PDF — try the top candidates, and VERIFY each is really this
    #    company before accepting (guards against same-keyword wrong companies).
    pdf_cands = sorted(
        [r for r in results if ".pdf" in r["href"].lower()],
        key=lambda r: _score_pdf(r["href"], r["title"], tokens), reverse=True,
    )
    chosen_url, data = None, None
    for r in pdf_cands[:8]:
        if _score_pdf(r["href"], r["title"], tokens) <= 0:
            break
        d = _download_pdf(r["href"])
        if d and _pdf_is_company(d, tokens) and _recent_enough(d):
            chosen_url, data = r["href"], d
            break

    # 3) Fallback: open the best issuer IR page(s) and harvest a VERIFIED report PDF.
    if chosen_url is None:
        pages = [r["href"] for r in results
                 if ".pdf" not in r["href"].lower()
                 and (not tokens or _host_matches(urlparse(r["href"]).netloc, tokens))]
        for page in pages[:3]:
            links = _page_pdf_links(page)
            ranked = sorted(links, key=lambda l: _score_pdf(l[0], l[1], tokens), reverse=True)
            for href, _txt in ranked[:10]:
                if _score_pdf(href, _txt, tokens) <= 0:
                    break
                d = _download_pdf(href)
                if d and _pdf_is_company(d, tokens) and _recent_enough(d):
                    chosen_url, data = href, d
                    break
            if chosen_url:
                break

    if chosen_url is None:
        raise LookupError(
            f"Could not locate a RECENT (>= {_min_year()}) annual-report PDF for "
            f"{label!r} via its IR site. Upload the PDF manually instead."
        )

    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    out_pdf_path.write_bytes(data)

    try:
        ocr_info = ocr_pdf.ensure_searchable_pdf(out_pdf_path, progress=ocr_progress)
    except Exception as e:
        ocr_info = {"ocr": False, "error": str(e)[:200]}

    yrs = _years(chosen_url.lower())
    return {
        "company_number": ticker,
        "company": name or ticker,
        "category": category,
        "form": "Annual Report / Financial Statements (IR site)",
        "filing_date": "",
        "report_period": f"{max(yrs)}-12-31" if yrs else "",
        "source_format": "application/pdf",
        "ocr": ocr_info,
        "url": chosen_url,
        "pdf_path": str(out_pdf_path),
        "pdf_size": out_pdf_path.stat().st_size if out_pdf_path.exists() else 0,
    }


if __name__ == "__main__":
    # Manual smoke test:  python ca_ir_fetch.py "Dollarama" DOL
    import json
    import sys
    nm = sys.argv[1] if len(sys.argv) > 1 else "Dollarama"
    tk = sys.argv[2] if len(sys.argv) > 2 else ""
    info = fetch_filing_as_pdf(tk, "annual", f"_test_ca_{(tk or nm)[:8]}.pdf", company_name=nm)
    print(json.dumps({k: v for k, v in info.items() if k != "ocr"}, indent=2, ensure_ascii=False))
