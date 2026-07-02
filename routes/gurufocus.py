"""
GuruFocus stock-URL resolver
============================

Turn a GuruFocus stock link into the inputs the EU tab's pipeline needs:

    https://www.gurufocus.com/stock/OSL:AUSS/summary
                                    ^^^ ^^^^
                                    exch ticker

  * ticker  — parsed straight from the URL.
  * country — from the exchange prefix via EXCHANGE_TO_COUNTRY (European regulated
              markets only; the EU tab is Europe-scoped).
  * company name — read from the page's <h1> / <title> in ONE lightweight request
              (GuruFocus is not bot-walled for a plain Chrome-impersonated GET).

Public API:
    parse_gurufocus_url(url)  -> (exchange, ticker) | None
    resolve(url)              -> {company_name, ticker, exchange, country, european}
"""
from __future__ import annotations

import re
from typing import Optional

# GuruFocus exchange code -> country, GLOBAL. Several common aliases per market.
# A code that isn't here resolves to country=None (the ticker still works for the
# dedicated tabs; the EU tab uses the European subset below to gate).
EXCHANGE_TO_COUNTRY = {
    # ── United States ──
    "NYSE": "United States", "NAS": "United States", "NASDAQ": "United States",
    "AMEX": "United States", "ASE": "United States", "NYSEAMERICAN": "United States",
    "NYSEMKT": "United States", "ARCA": "United States", "BATS": "United States",
    "IEX": "United States", "NMS": "United States", "NCM": "United States",
    "NGM": "United States", "OTC": "United States", "OTCMKTS": "United States",
    "OTCPK": "United States", "OTCQX": "United States", "OTCQB": "United States",
    "PINX": "United States", "PNK": "United States",
    # ── Canada ──
    "TSX": "Canada", "TSXV": "Canada", "CVE": "Canada", "NEO": "Canada", "CNSX": "Canada",
    # ── Europe — Nordics ──
    "OSL": "Norway",
    "STO": "Sweden", "OM": "Sweden", "XSTO": "Sweden",
    "HEL": "Finland", "XHEL": "Finland",
    "CPH": "Denmark", "OCSE": "Denmark", "XCSE": "Denmark",
    "ICSE": "Iceland", "XICE": "Iceland",
    # ── Europe — DACH ──
    "XTER": "Germany", "XETRA": "Germany", "ETR": "Germany", "FRA": "Germany",
    "GER": "Germany", "STU": "Germany", "MUN": "Germany", "BER": "Germany",
    "WBO": "Austria", "VIE": "Austria", "XWBO": "Austria",
    # ── Europe — Euronext ──
    "XPAR": "France", "PAR": "France", "EPA": "France",
    "XAMS": "Netherlands", "AMS": "Netherlands",
    "XBRU": "Belgium", "BRU": "Belgium", "EBR": "Belgium",
    "XLIS": "Portugal", "LIS": "Portugal", "ELI": "Portugal",
    "ISE": "Ireland", "XDUB": "Ireland", "DUB": "Ireland",
    # ── Europe — UK ──
    "LSE": "United Kingdom", "XLON": "United Kingdom", "LON": "United Kingdom",
    # ── Europe — Southern ──
    "MIL": "Italy", "BIT": "Italy", "XMIL": "Italy",
    "BME": "Spain", "XMAD": "Spain", "MCE": "Spain", "MAD": "Spain",
    "ATH": "Greece", "XATH": "Greece",
    # ── Europe — Central / Eastern ──
    "WAR": "Poland", "WSE": "Poland", "XWAR": "Poland",
    "BUD": "Hungary", "XBUD": "Hungary",
    "PRA": "Czechia", "XPRA": "Czechia",
    "LJSE": "Slovenia", "XLJU": "Slovenia",
    "BVB": "Romania", "XBSE": "Romania",
    "RSE": "Latvia", "TLSE": "Estonia", "VSE": "Lithuania",
    "ZSE": "Croatia", "XZAG": "Croatia",
    "MTSE": "Malta", "XMAL": "Malta",
    "CYSE": "Cyprus",
    "LUX": "Luxembourg", "XLUX": "Luxembourg",
    # ── Switzerland (not EU/EEA but European) ──
    "SWX": "Switzerland", "XSWX": "Switzerland", "VTX": "Switzerland", "EBS": "Switzerland",
    # ── Asia-Pacific ──
    "TSE": "Japan", "TYO": "Japan", "JPX": "Japan",          # Tokyo
    "SHSE": "China", "SZSE": "China", "SHA": "China", "SZA": "China", "SSE": "China",
    "HKSE": "Hong Kong", "HKG": "Hong Kong", "SEHK": "Hong Kong",
    "NSE": "India", "BOM": "India", "BSE": "India",
    "XKRX": "South Korea", "KRX": "South Korea", "KOSE": "South Korea",
    "KOSDAQ": "South Korea", "KSC": "South Korea",
    "TPE": "Taiwan", "ROCO": "Taiwan", "TAI": "Taiwan", "TWSE": "Taiwan",
    "IDX": "Indonesia", "JKT": "Indonesia",
    "KLSE": "Malaysia", "XKLS": "Malaysia", "KL": "Malaysia",
    "BKK": "Thailand", "SET": "Thailand",
    "SGX": "Singapore", "SES": "Singapore",
    "ASX": "Australia",
    # ── Americas (non-US) ──
    "BSP": "Brazil", "SAO": "Brazil", "BOVESPA": "Brazil", "BVMF": "Brazil",
    "MEX": "Mexico", "BMV": "Mexico",
    # ── Middle East ──
    "TLV": "Israel", "TASE": "Israel",
}

# US exchange codes — for these we DON'T scrape the page for the company name
# (US listings resolve fine by ticker alone, e.g. via SEC EDGAR).
US_EXCHANGES = {
    "NYSE", "NAS", "NASDAQ", "AMEX", "ASE", "NYSEAMERICAN", "NYSEMKT", "ARCA",
    "BATS", "IEX", "NMS", "NCM", "NGM", "OTC", "OTCMKTS", "OTCPK", "OTCQX",
    "OTCQB", "PINX", "PNK",
}

# EU/EEA countries — used by the Europe tab's `european` gate (Switzerland and the
# UK are European but NOT EU/EEA-ESEF markets, so they're excluded here).
_EU_EEA_COUNTRIES = {
    "Norway", "Sweden", "Finland", "Denmark", "Iceland", "Germany", "Austria",
    "France", "Netherlands", "Belgium", "Portugal", "Ireland", "Italy", "Spain",
    "Greece", "Poland", "Hungary", "Czechia", "Slovenia", "Romania", "Latvia",
    "Estonia", "Lithuania", "Croatia", "Malta", "Cyprus", "Luxembourg",
}

_URL_RE = re.compile(r"gurufocus\.com/stock/([^/?#]+)", re.I)
_UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def parse_gurufocus_url(url: str) -> Optional[tuple[str, str]]:
    """Return (exchange, ticker) from a GuruFocus stock URL, or None.
    The symbol token may be 'OSL:AUSS' (exchange:ticker) or a bare ticker."""
    m = _URL_RE.search(url or "")
    if not m:
        return None
    token = m.group(1).strip()
    if ":" in token:
        ex, tk = token.split(":", 1)
        return ex.strip().upper(), tk.strip().upper()
    return "", token.strip().upper()


def fetch_company_name(url: str) -> Optional[str]:
    """Read the issuer's name from the GuruFocus page (<h1>, else og:title/title).
    One Chrome-impersonated GET. Returns None on any failure."""
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=20)
        if r.status_code != 200 or not r.text:
            return None
        html = r.text
    except Exception:
        return None

    # <h1> is the cleanest ("Austevoll Seafood ASA"); fall back to og:title/title and
    # strip the trailing "(EXCH:TICKER) ... | GuruFocus" decoration.
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    if m:
        name = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if name:
            return _clean_name(name)
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                  html, re.I)
    if not m:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        return _clean_name(m.group(1))
    return None


def _clean_name(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "")).strip()
    # drop "(OSL:AUSS) ..." and anything after a pipe ("... | GuruFocus")
    s = re.split(r"\s*\(", s)[0]
    s = re.split(r"\s*\|", s)[0]
    return s.strip()


def resolve(url: str) -> dict:
    """Resolve a GuruFocus stock URL to the pipeline's inputs.

    Returns:
        {company_name, ticker, exchange, country, european, is_us}
    Raises ValueError if the URL is not a GuruFocus stock link.

    - `ticker` + `exchange` come from the URL (no scrape).
    - `country` from the exchange map (None if unknown).
    - `is_us`: a US listing — we DON'T scrape the page for the name (US resolves
      fine by ticker alone, e.g. via SEC EDGAR), so company_name is None.
    - `european`: the country is an EU/EEA market (the Europe tab uses this to
      gate; other tabs ignore it).
    For all non-US exchanges we read the company name from the page (the IR-scraper
    resolver needs it for non-US tickers).
    """
    parsed = parse_gurufocus_url(url)
    if not parsed:
        raise ValueError("Not a GuruFocus stock URL")
    exchange, ticker = parsed
    country = EXCHANGE_TO_COUNTRY.get(exchange)
    is_us = exchange in US_EXCHANGES or country == "United States"
    european = country in _EU_EEA_COUNTRIES
    # Scrape the company name for every market EXCEPT the US (ticker-only there).
    company_name = None if is_us else fetch_company_name(url)
    return {
        "company_name": company_name,
        "ticker": ticker,
        "exchange": exchange,
        "country": country,
        "european": european,
        "is_us": is_us,
    }


if __name__ == "__main__":
    import json
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else "https://www.gurufocus.com/stock/OSL:AUSS/summary"
    print(json.dumps(resolve(u), ensure_ascii=False, indent=2))
