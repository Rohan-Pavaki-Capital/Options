"""
diamond_route.py — the "Diamond" flagship router.

Given only a company NAME + TICKER (market doesn't matter), find the latest
annual report / financial-statements PDF by trying, in order:

  1. the country's DEDICATED integration (authoritative, deterministic) — routed
     from a best-effort Wikidata country lookup;
  2. SEC EDGAR (10-K / 20-F / 40-F) — broad coverage for US listings + the many
     foreign issuers that file ADR reports with the SEC;
  3. the universal IR-scraper fallback (resolve the issuer's own IR site →
     download its latest annual report), for everything not covered above.

The first attempt that yields a valid PDF wins. Returns a metadata dict with
`diamond_source` set to the path that succeeded, or raises RuntimeError listing
every attempt's failure.

This module is import-safe: it reuses the SAME fetch/resolve functions the rest
of backend.py uses, so the dedicated tabs are unaffected.
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Callable, Optional
import requests
import fitz  # PyMuPDF
from rapidfuzz import fuzz

# Dedicated fetchers/resolvers (same modules backend.py imports)
from edgar_fetch import fetch_filing_as_pdf as _edgar
from ca_fetch import fetch_filing_as_pdf as _ca
from japan_fetch import fetch_filing_as_pdf as _jp
from jp_resolve import resolve_company_number as _r_jp
from kr_fetch import fetch_filing_as_pdf as _kr
from kr_resolve import resolve_company_number as _r_kr
from br_fetch import fetch_filing_as_pdf as _br
from br_resolve import resolve_company_number as _r_br
from tw_fetch import fetch_filing_as_pdf as _tw
from tw_resolve import resolve_company_number as _r_tw
from cn_fetch import fetch_filing_as_pdf as _cn
from cn_resolve import resolve_company_number as _r_cn
from in_fetch import fetch_filing_as_pdf as _in
from in_resolve import resolve_company_number as _r_in
from hk_fetch import fetch_filing_as_pdf as _hk
from hk_resolve import resolve_company_number as _r_hk
from id_fetch import fetch_filing_as_pdf as _id
from il_fetch import fetch_filing_as_pdf as _il
from il_resolve import resolve_company_number as _r_il
from eu_fetch import fetch_filing_as_pdf as _eu
from eu_resolve import resolve_company_number as _r_eu
from companies_house_fetch import fetch_filing_as_pdf as _uk
from uk_resolve import resolve_company_number as _r_uk
from denmark_fetch import fetch_filing_as_pdf as _dk
from dk_resolve import resolve_company_number as _r_dk

UA = "options-extractor-diamond/0.1"

# country (lowercased) -> source key
COUNTRY_TO_SOURCE = {
    "united states": "edgar", "united states of america": "edgar", "usa": "edgar", "u.s.": "edgar",
    "canada": "canada",
    "united kingdom": "uk", "uk": "uk", "great britain": "uk",
    "japan": "japan",
    "south korea": "korea", "korea": "korea", "republic of korea": "korea",
    "china": "china", "people's republic of china": "china",
    "hong kong": "hongkong",
    "taiwan": "taiwan", "republic of china (taiwan)": "taiwan",
    "india": "india",
    "indonesia": "indonesia",
    "brazil": "brazil",
    "israel": "israel",
    "denmark": "denmark",
    # EU/EEA members covered by the pan-EU ESEF source (Germany/Ireland are NOT in
    # the repo -> they fall through to the IR-scraper, which is correct).
    "france": "eu", "netherlands": "eu", "the netherlands": "eu", "spain": "eu", "italy": "eu",
    "sweden": "eu", "finland": "eu", "belgium": "eu", "austria": "eu", "portugal": "eu",
    "poland": "eu", "greece": "eu", "luxembourg": "eu", "norway": "eu", "iceland": "eu",
    "croatia": "eu", "hungary": "eu", "romania": "eu", "slovakia": "eu", "slovenia": "eu",
    "estonia": "eu", "latvia": "eu", "lithuania": "eu", "cyprus": "eu", "malta": "eu",
}


def _valid_pdf(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size < 2000:
            return False
        with open(path, "rb") as f:
            if f.read(4) != b"%PDF":
                return False
        d = fitz.open(path)
        return d.page_count >= 1
    finally:
        pass


def detect_country(company_name: str, ticker: str) -> Optional[str]:
    """Best-effort issuer country via Wikidata (name search -> entity -> P17 label).
    Returns a lowercased country name or None. Never raises."""
    try:
        r = requests.get(
            "https://www.wikidata.org/w/api.php",
            params={"action": "wbsearchentities", "search": company_name,
                    "language": "en", "type": "item", "limit": 3, "format": "json"},
            headers={"User-Agent": UA}, timeout=12)
        if r.status_code != 200:
            return None
        for hit in r.json().get("search", []):
            qid = hit.get("id")
            if not qid:
                continue
            c = requests.get("https://www.wikidata.org/w/api.php",
                             params={"action": "wbgetentities", "ids": qid,
                                     "props": "claims", "format": "json"},
                             headers={"User-Agent": UA}, timeout=12)
            claims = c.json().get("entities", {}).get(qid, {}).get("claims", {})
            p17 = claims.get("P17")
            if not p17:
                continue
            country_qid = p17[0].get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
            if not country_qid:
                continue
            lab = requests.get("https://www.wikidata.org/w/api.php",
                               params={"action": "wbgetentities", "ids": country_qid,
                                       "props": "labels", "languages": "en", "format": "json"},
                               headers={"User-Agent": UA}, timeout=12)
            name = (lab.json().get("entities", {}).get(country_qid, {})
                    .get("labels", {}).get("en", {}).get("value"))
            if name:
                return name.strip().lower()
    except Exception:
        return None
    return None


# ── per-source attempt wrappers (each raises on failure) ─────────────
def _name_matches(requested: str, resolved: str) -> bool:
    """True if the resolved company name plausibly matches what the user asked for.
    No requested name -> can't verify, accept. Guards against ticker collisions
    (e.g. a ticker resolving to an unrelated fund instead of the named issuer)."""
    if not requested:
        return True
    return fuzz.token_set_ratio(requested.lower(), (resolved or "").lower()) >= 55


def _edgar_name_ticker(name: str) -> Optional[str]:
    """Resolve a company NAME -> SEC ticker via EDGAR full-text company search,
    only if the top hit's name actually matches. Returns ticker or None."""
    try:
        from edgar import find_company, set_identity
        set_identity(os.environ.get("EDGAR_IDENTITY", "options-extractor test@example.com"))
        res = find_company(name)
        if getattr(res, "empty", True):
            return None
        top = res[0]
        tk = (getattr(top, "tickers", None) or [None])[0]
        if tk and _name_matches(name, getattr(top, "name", "")):
            return tk
    except Exception:
        return None
    return None


def _attempt_edgar(name, ticker, out, cat, prog):
    # Build candidate tickers: a NAME-verified one first (so a wrong/blank ticker
    # can't win), then the user's raw ticker.
    candidates: list[str] = []
    if name:
        nt = _edgar_name_ticker(name)
        if nt:
            candidates.append(nt)
    if ticker and ticker.upper() not in {c.upper() for c in candidates}:
        candidates.append(ticker)

    last = None
    for tk in candidates:
        for form in ("10-K", "20-F", "40-F"):
            try:
                info = _edgar(ticker=tk, form=form, out_pdf_path=out)
                # Reject a match whose company name isn't the one requested.
                if not _name_matches(name, info.get("company") or ""):
                    last = RuntimeError(
                        f"EDGAR '{tk}' -> '{info.get('company')}' != '{name}'")
                    try:
                        Path(out).unlink()
                    except Exception:
                        pass
                    break  # wrong entity for this ticker; skip its other forms
                if _valid_pdf(Path(out)):
                    return {**info, "diamond_source": "edgar"}
            except Exception as e:
                last = e
    raise last or RuntimeError("EDGAR: no matching 10-K/20-F/40-F found")


def _attempt_canada(name, ticker, out, cat, prog):
    info = _ca(ticker=ticker, category="annual", out_pdf_path=out,
               company_name=name, ocr_progress=prog)
    return {**info, "diamond_source": "canada"}


def _attempt_indonesia(name, ticker, out, cat, prog):
    info = _id(company_number=ticker, category=cat, out_pdf_path=out,
               company_name=name, ocr_progress=prog)
    return {**info, "diamond_source": "indonesia"}


def _make_std(src, resolve_fn, fetch_fn):
    """Standard resolve(ticker,name)->company_number then fetch(company_number=...)."""
    def attempt(name, ticker, out, cat, prog):
        num = resolve_fn(ticker, name or None)["company_number"]
        info = fetch_fn(company_number=num, category=cat, out_pdf_path=out,
                        company_name=name, ocr_progress=prog)
        return {**info, "diamond_source": src}
    return attempt


def _attempt_eu(name, ticker, out, cat, prog):
    num = _r_eu(ticker, name or None, None, None)["company_number"]
    info = _eu(company_number=num, category=cat, out_pdf_path=out,
               company_name=name, ocr_progress=prog)
    return {**info, "diamond_source": "eu"}


def _attempt_irscraper(name, ticker, out, cat, prog, country=None):
    import ir_resolve_proto as R
    import ir_fetch_proto as F
    res = R.resolve(name or "", ticker or "", "", country or "")
    ir_url = res.get("chosen_url")
    if not ir_url:
        guess = res.get("low_conf_guess")
        if guess:
            raise RuntimeError(
                f"could not confidently identify the company's IR site "
                f"(best guess was {guess}, but confidence was too low to trust). "
                f"Refusing to extract from a possibly-wrong company — "
                f"try the per-market tab or the Upload tab.")
        raise RuntimeError("IR-scraper: could not resolve an IR site")
    out_result = F.fetch_annual_report(ir_url, allow_fc=True, save_path=str(out), name=name or "")
    if not out_result or not _valid_pdf(Path(out)):
        raise RuntimeError(f"IR-scraper: no gate-passing annual report at {ir_url}")
    info = out_result.get("info", {})
    return {"company": name, "form": "Annual Report",
            "report_period": out_result.get("fiscal_year"),
            "ir_url": ir_url, "resolver_confidence": res.get("confidence"),
            "pages": info.get("pages"), "diamond_source": "ir_scraper"}


_ATTEMPTS = {
    "edgar": _attempt_edgar,
    "canada": _attempt_canada,
    "indonesia": _attempt_indonesia,
    "eu": _attempt_eu,
    "japan": _make_std("japan", _r_jp, _jp),
    "korea": _make_std("korea", _r_kr, _kr),
    "china": _make_std("china", _r_cn, _cn),
    "hongkong": _make_std("hongkong", _r_hk, _hk),
    "taiwan": _make_std("taiwan", _r_tw, _tw),
    "india": _make_std("india", _r_in, _in),
    "brazil": _make_std("brazil", _r_br, _br),
    "israel": _make_std("israel", _r_il, _il),
    "denmark": _make_std("denmark", _r_dk, _dk),
    "uk": _make_std("uk", _r_uk, _uk),
}


def fetch_for_diamond(
    company_name: str,
    ticker: str,
    out_pdf_path,
    category: str = "annual",
    progress: Optional[Callable[[int, int], None]] = None,
    log: Optional[Callable[[str], None]] = None,
    country: Optional[str] = None,
    allow_edgar_fallback: bool = True,
) -> dict:
    """COUNTRY-ROUTED Diamond (per user request):
      1. If the user-supplied `country` maps to a DEDICATED data-API source
         (COUNTRY_TO_SOURCE — e.g. United States -> SEC EDGAR, Japan -> EDINET, …),
         fetch via that authoritative integration. If it fails, fall back to the scraper
         (so a dedicated miss never dead-ends).
      2. Otherwise (no country given, or a country with no dedicated source), use the
         universal IR-scraper — resolve the issuer's IR site and download its report.
    Returns metadata (with `diamond_source`) or raises with every attempt's error."""
    out = Path(out_pdf_path)
    say = log or (lambda m: None)
    company_name = (company_name or "").strip()
    ticker = (ticker or "").strip()
    country = (country or "").strip()
    errors: list[str] = []

    # 1) Dedicated data-API route — only when the user gave a country that has one.
    src = COUNTRY_TO_SOURCE.get(country.lower()) if country else None
    if src and src in _ATTEMPTS:
        say(f"Diamond: country='{country}' -> dedicated source '{src}'")
        try:
            if out.exists():
                out.unlink()
            info = _ATTEMPTS[src](company_name, ticker, out, category, progress)
            if _valid_pdf(out):
                say(f"OK via {src}")
                return info
            errors.append(f"{src}: produced no valid PDF")
        except Exception as e:
            errors.append(f"{src}: {e}")
        say(f"dedicated source '{src}' did not yield a report; falling back to IR-scraper")
    elif country:
        say(f"Diamond: country='{country}' has no dedicated source -> IR-scraper")

    # 2) IR-scraper (primary for uncovered countries; fallback otherwise).
    # Prefer the user-supplied country as the resolver hint; else best-effort detect it.
    hint = country or None
    if not hint:
        try:
            hint = detect_country(company_name, ticker)
            if hint:
                say(f"Detected issuer country: {hint}")
        except Exception:
            hint = None
    say("Diamond: resolving the issuer's IR site (scraper)")
    try:
        if out.exists():
            out.unlink()
        info = _attempt_irscraper(company_name, ticker, out, category, progress, hint)
        say("OK via ir_scraper")
        return info
    except Exception as e:
        errors.append(f"ir_scraper: {e}")

    # 3) LAST-RESORT: SEC EDGAR (10-K / 20-F / 40-F), name-verified. Covers US/ADR filers
    # whose report is an SEC filing not downloadable from their own IR site (e.g. Apple,
    # NeoGenomics — their Q4 IR pages don't serve real report PDFs). A 10-K is an acceptable
    # result. Skipped if EDGAR was already the dedicated route tried above.
    # Skipped when the caller disables it (e.g. the Singapore tab is locked to SGX
    # issuers — a US EDGAR match on the company name would be a wrong-entity result).
    if src != "edgar" and allow_edgar_fallback:
        say("Diamond: last-resort SEC EDGAR (10-K/20-F/40-F)")
        try:
            if out.exists():
                out.unlink()
            info = _attempt_edgar(company_name, ticker, out, category, progress)
            if _valid_pdf(out):
                say("OK via edgar (fallback)")
                return info
            errors.append("edgar: produced no valid PDF")
        except Exception as e:
            errors.append(f"edgar: {e}")
    elif not allow_edgar_fallback:
        say("Diamond: EDGAR fallback disabled by caller (no wrong-entity US filing)")
        raise RuntimeError(
            "Could not find an annual report on this company's investor-relations "
            "site. Please use the Upload tab to submit the PDF directly. "
            "(details: " + " | ".join(errors) + ")"
        )

    raise RuntimeError("Diamond could not fetch a report: " + " | ".join(errors))
