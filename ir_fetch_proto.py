"""
ir_fetch_proto.py  —  STANDALONE prototype of the Hybrid (C) annual-report finder.

Takes a resolved IR-page URL (from ir_resolve_proto.resolve) and:
  1. crawl  : plain-HTTP fetch + link extract; Firecrawl render only as fallback
  2. expand : follow the best "reports / financial statements" sub-page (1 hop)
  3. score  : rank candidate PDFs by the doc-selection rubric (encodes the
              Indonesia/Canada lessons — prefer AUDITED financial statements,
              reject interim/ESG/proxy/presentation, recency guard)
  4. inspect: download top candidate, open with PyMuPDF, scan for share-based-
              payment terms  ==  a stand-in for "Stage-1 would accept this".
              (In production this IS Stage-1; 0 SBC pages -> reject, try next.)

Touches nothing in the running app. Firecrawl is opt-in (--firecrawl) to save credits.
"""
from __future__ import annotations
import re, sys, io, time
from urllib.parse import urljoin, urlparse, unquote
import requests
from bs4 import BeautifulSoup
import fitz  # PyMuPDF
try:
    from dotenv import load_dotenv
    load_dotenv()  # fc_client._key() reads FIRECRAWL_API_KEY from the environment
except Exception:
    pass

UA = "Mozilla/5.0 (options-extractor-ir-fetch/0.1)"
TIMEOUT = 20
CURRENT_YEAR = 2026  # from project context; FY2025 reports are the latest expected
# Hard freshness floor: reject any report older than this fiscal year (per request —
# "max old year should be 2024, not older than that"). At CURRENT_YEAR=2026 that's 2024.
MIN_FISCAL_YEAR = CURRENT_YEAR - 2

# Playwright render tuning (latency control). "load" waits for ALL resources and
# routinely burns the full timeout on slow IR sites; "domcontentloaded" is enough to
# harvest links and fires in ~2-3s. Cap escalation pages + reuse ONE browser.
PW_GOTO_TIMEOUT_MS = 9_000
PW_IDLE_TIMEOUT_MS = 2_500
PW_SCROLLS = 2
PW_MAX_PAGES = 2            # only render the most relevant pages, not every crawled one
FETCH_BUDGET_SEC = 35      # soft wall-clock budget: skip the slow paid Firecrawl tier past this
_UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# interim/quarterly report name pattern — used by fetch_reports() to pick interim
# candidates by NAME (the doc-type rubric deliberately scores them negative, so they
# can't be found by score). The content gate (inspect_pdf) still verifies recency.
_INTERIM_NAME_RE = (r"interim|half[- ]?year|\bh[12]\b|\bq[1-4]\b|quarter|"
                    r"first[- ]quarter|second[- ]quarter|third[- ]quarter|"
                    r"six months|nine months|three months|"
                    r"半年|中期|季度|季報|季报")

# share-based-payment terms (subset of keywords.py) — acceptance probe (EN + CJK)
SBC_TERMS = [
    "share-based payment", "share based payment", "stock option", "share option",
    "stock-based compensation", "equity-settled", "options outstanding",
    "exercise price", "vesting", "restricted stock", "rsu", "esop", "grant date",
    # Chinese (Traditional/Simplified): share-based payment / equity incentive / option / vesting
    "股份支付", "股權激勵", "股权激励", "以股份為基礎", "購股權", "认股权", "認股權",
    "限制性股票", "受限制股份", "期權", "归属", "歸屬",
]

# POS = doc-type signals. At least one POS hit is REQUIRED (recency/English alone can't win).
POS = [
    (r"consolidated financial statement", 40), (r"audited", 35),
    (r"annual financial report|annual financial statement", 38),
    (r"financial statement", 32),
    (r"\bafs\b|[-_ ]fs[-_]|[-_ ]fs\b|financial.?stmt", 30),   # FS / AFS abbreviations
    (r"annual report|annual[-_ ]?report|[-_ ]ar[-_]20", 30),
    (r"form\s*10-?k|form\s*20-?f|form\s*40-?f", 35),
    (r"\b10-?k\b|\b20-?f\b|\b40-?f\b", 25),
    (r"annual information form|\baif\b", 12),                 # Canadian AIF: annual but not the FS
    (r"\breport[-_ ]?(?:fy)?20\d{2}|(?:fy)?20\d{2}[-_ ]?report|integrated report|group report", 22),  # "Siemens Report FY2025" style
    (r"\bfy20\d{2}\b", 8),
    # CJK: annual report / financial statements (Traditional + Simplified)
    (r"年度報告|年度报告|年報|年报", 32),
    (r"綜合財務報表|合併財務報表|财务报表|財務報表|財務報告|财务报告", 34),
]
NEG = [
    # interim — incl. compact forms Q3FY26 / FY26Q3 / 3Q26
    (r"interim|half-?year|\bquarter|first quarter|third quarter|6 months|"
     r"\bq[1-4]\b|q[1-4]\s*fy|fy\s*\d{2}\s*q[1-4]|[1-4]q\d{2}|q[1-4]\d{2}|q[1-4]fy", 45),
    (r"\bmd&?a\b|management discussion", 25),                 # MD&A alone is not the statements
    (r"tender|offer to purchase|prospectus|supplement", 40),
    (r"esg|sustainab|\bcsr\b|climate|carbon|diversity|impact report", 45),
    (r"proxy|circular|\bagm\b|notice of meeting|information statement|voting", 30),
    (r"present|slides|transcript|webcast|fact ?sheet|infographic|fireside", 35),
    (r"summary|highlights|press release|news release|\bpr\b|media|alert", 22),
    (r"governance|remuneration report|compensation discussion", 12),
    # HKEX/SEHK periodic regulatory returns — NOT the annual report
    (r"disclosure return|monthly return|equity issuer|movements? in|next day|"
     r"poll result|notifiable|connected transaction|proxy form|notice of|nomination", 45),
    # CJK negatives: interim / quarterly / half-year / announcement / circular / presentation / ESG
    (r"中期報告|中期报告|中期業績|中期业绩|季度報告|季度报告|半年報|半年报|季報|季报", 45),
    (r"公告|通函|簡報|简报|演示|業績發布|业绩发布", 25),
    (r"環境.{0,4}社會|环境.{0,4}社会|可持續|可持续|永續|永续|\besg\b", 40),
]
REPORTS_PAGE = [
    (r"annual[- ]?publication", 36), (r"annual[- ]?report", 32),
    (r"annual[- ]?result", 26), (r"financial statement", 30), (r"financial report", 25),
    (r"reports? (and|&) (filing|presentation|document|publication)", 22),
    (r"financials\b", 20), (r"\bfilings?\b", 16), (r"reports?\b", 10), (r"investor", 6),
]
# steer the crawl AWAY from interim/news pages when picking which sub-page to follow
REPORTS_PAGE_NEG = [
    (r"quarter|interim|\bq[1-4]\b|half-?year", 30),
    (r"news|press|media|event|presentation|webcast", 22),
    (r"governance|esg|sustainab", 20),
]


def _http_links(url: str) -> tuple[list[tuple[str, str]], str]:
    """Return [(abs_url, anchor_text), ...] and page text. Fetches with curl_cffi
    (real Chrome TLS fingerprint) so bot-walled-but-static IR pages (e.g. Manulife)
    return their HTML+links WITHOUT needing Firecrawl; falls back to plain requests."""
    r = None
    try:
        from curl_cffi import requests as _creq
        r = _creq.get(url, impersonate="chrome", timeout=TIMEOUT, allow_redirects=True)
    except Exception:
        r = None
    if r is None or r.status_code >= 400:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    base = str(getattr(r, "url", url)) or url
    out = []
    for a in soup.find_all("a", href=True):
        out.append((urljoin(base, a["href"]), " ".join(a.get_text().split())[:120]))
    return out, soup.get_text(" ", strip=True)[:5000]


def _fc_links(url: str) -> tuple[list[tuple[str, str]], str]:
    """Firecrawl fallback for JS / bot-walled IR pages. Recovers anchor TEXT from the
    markdown ([text](url)) — the `links` format alone is bare URLs (no doc-type signal,
    which is why opaque-UUID sites like Alibaba abstained)."""
    import fc_client
    data = fc_client.scrape(url, formats=("links", "markdown"))
    md = data.get("markdown") or ""
    anchor: dict[str, str] = {}
    for m in re.finditer(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", md):
        text, u = " ".join(m.group(1).split()), m.group(2).strip().rstrip(").,")
        if u not in anchor or len(text) > len(anchor[u]):
            anchor[u] = text
    bare = data.get("links", [])
    links = [(u, anchor.get(u, "")) for u in bare]
    for u, t in anchor.items():        # markdown-only links not in links[]
        if u not in bare:
            links.append((u, t))
    return links, md[:5000]


def _pw_links_multi(urls: list[str]) -> dict[str, tuple[list[tuple[str, str]], str]]:
    """Render several JS pages in ONE headless-Chromium session (launching a browser
    per page is the slow part). Uses `domcontentloaded` + short timeouts so a slow IR
    site can't burn 20s/page. Harvests each anchor's resolved href + text — recovering
    the PDF links static HTML misses. FREE; does NOT defeat bot walls (-> Firecrawl)."""
    from playwright.sync_api import sync_playwright

    out: dict[str, tuple[list[tuple[str, str]], str]] = {u: ([], "") for u in urls}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            ctx = browser.new_context(user_agent=_UA_CHROME)
            for url in urls:
                page = None
                try:
                    page = ctx.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=PW_GOTO_TIMEOUT_MS)
                    try:
                        page.wait_for_load_state("networkidle", timeout=PW_IDLE_TIMEOUT_MS)
                    except Exception:
                        pass  # report lists may never idle; the DOM is enough
                    try:
                        for _ in range(PW_SCROLLS):
                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            page.wait_for_timeout(500)
                    except Exception:
                        pass
                    anchors = page.eval_on_selector_all(
                        "a[href]",
                        "els => els.map(e => [e.href, (e.textContent || '').trim().slice(0, 120)])",
                    )
                    links = [(a[0], " ".join((a[1] or "").split())) for a in anchors if a and a[0]]
                    try:
                        text = (page.inner_text("body") or "")[:5000]
                    except Exception:
                        text = ""
                    out[url] = (links, text)
                except Exception as e:
                    print(f"    [playwright err {url[:55]}: {e}]", file=sys.stderr)
                finally:
                    if page is not None:
                        try:
                            page.close()
                        except Exception:
                            pass
        finally:
            browser.close()
    return out


def _pw_links(url: str) -> tuple[list[tuple[str, str]], str]:
    """Single-URL convenience wrapper over _pw_links_multi."""
    return _pw_links_multi([url]).get(url, ([], ""))


def get_links(url: str, allow_fc: bool, force_fc: bool = False,
              force_pw: bool = False) -> tuple[list[tuple[str, str]], str, str]:
    # Escalation tier: local Playwright render (free) — used when the static crawl
    # found no usable PDF link (JS-rendered listings).
    if force_pw:
        try:
            links, text = _pw_links(url)
            return links, text, "playwright"
        except Exception as e:
            print(f"    [playwright err: {e}]", file=sys.stderr)
            return [], "", "none"
    if not force_fc:
        try:
            links, text = _http_links(url)
            if len(links) >= 5:
                return links, text, "http"
        except Exception as e:
            print(f"    [http err: {e}]", file=sys.stderr)
        # Prefer local Playwright (fast, free) over Firecrawl when static HTML is thin
        # (JS-rendered landing pages). Firecrawl stays the last resort for bot walls.
        try:
            links, text = _pw_links(url)
            if len(links) >= 5:
                return links, text, "playwright"
        except Exception as e:
            print(f"    [playwright err: {e}]", file=sys.stderr)
    if allow_fc:
        try:
            links, text = _fc_links(url)
            return links, text, "firecrawl"
        except Exception as e:
            print(f"    [firecrawl err: {e}]", file=sys.stderr)
    return [], "", "none"


def _pdf_year(u: str, anchor: str) -> int:
    return _year_in(unquote(urlparse(u).path) + " " + anchor) or 0


def _year_in(s: str) -> int | None:
    # not \b-bounded: CJK chars are word-chars, so "2025年" has no boundary after 2025
    yrs = [int(y) for y in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", s)]
    # also catch FY26 / FY2026 style
    for m in re.findall(r"fy\s*'?(\d{2,4})", s):
        yrs.append(2000 + int(m) if len(m) == 2 else int(m))
    return max(yrs) if yrs else None


def score_pdf(url: str, anchor: str) -> float:
    path = unquote(urlparse(url).path)            # decode %E8%B2%A1.. -> 財.. so CJK terms match
    blob = f"{anchor} {path}".lower()
    if ".pdf" not in blob and "download" not in blob and "/doc" not in blob and "ecms-files" not in blob:
        return -100
    pos = sum(w for pat, w in POS if re.search(pat, blob))
    neg = sum(w for pat, w in NEG if re.search(pat, blob))
    s = pos - neg
    if re.search(r"\ben\b|english|/en[-/]", blob):
        s += 8
    y = _year_in(blob)
    if y is not None:
        # graded + monotonic: newer always outranks older (was flat -25 for all old years,
        # which let a 9-year-old Siemens AR2016 tie the latest and win the sort arbitrarily)
        s += max(-40, 25 - 7 * (CURRENT_YEAR - y))
    # GATE: with no doc-type signal, recency/English can't manufacture a winner.
    if pos == 0:
        s = min(s, 5)
    return s


def find_report_pdfs(ir_url: str, allow_fc: bool) -> list[tuple[float, str, str]]:
    t0 = time.time()
    pdfs = {}

    def add(u, anchor):
        if re.search(r"\.pdf(\?|$)", u, re.I) or re.search(r"download|/doc|getfile|ecms-files", u, re.I):
            sc = score_pdf(u, anchor)
            if u not in pdfs or sc > pdfs[u][0]:
                pdfs[u] = (sc, anchor)

    def crawl_page(u, force_fc=False, force_pw=False):
        links, _, via = get_links(u, allow_fc, force_fc=force_fc, force_pw=force_pw)
        print(f"    crawl via {via}: {len(links)} links on {u[:70]}")
        for su, sa in links:
            add(su, sa)
        return links

    crawled = [ir_url]
    links = crawl_page(ir_url)

    # follow the best "reports / financial statements" sub-pages
    cand_pages = []
    for u, a in links:
        blob = f"{a} {urlparse(u).path}".lower()
        ps = sum(w for pat, w in REPORTS_PAGE if re.search(pat, blob))
        ps -= sum(w for pat, w in REPORTS_PAGE_NEG if re.search(pat, blob))
        if ps > 0 and urlparse(u).netloc and not re.search(r"\.pdf", u, re.I):
            cand_pages.append((ps, u, a))
    cand_pages.sort(reverse=True)
    seen = {ir_url}
    for ps, u, a in cand_pages[:3]:
        if u in seen:
            continue
        seen.add(u)
        crawled.append(u)
        print(f"    -> follow reports page (score {ps}): {u}")
        crawl_page(u)

    def _assess():
        # judge staleness ONLY from report-like candidates (score>=20 = has a doc-type
        # signal), else stray recent PDFs (AGM notices, sustainability reports) mask a
        # stale archive and escalation never fires (the Siemens FY2020 bug).
        best = max((sc for sc, _ in pdfs.values()), default=-100)
        newest = max((_pdf_year(u, a) for u, (sc, a) in pdfs.items() if sc >= 20), default=0)
        return best, newest, bool(newest and newest < CURRENT_YEAR - 1)

    # ESCALATION when the static HTTP crawl yields only signal-less PDFs (opaque filenames
    # / JS-rendered labels) OR only STALE reports (the latest report sits in a JS-rendered
    # section the static HTML missed). Tier 1 = local Playwright render (FREE); Tier 2 =
    # Firecrawl stealth (paid) only if Playwright still came up short AND credits are allowed.
    best, newest, stale = _assess()
    if best <= 5 or stale:
        why = "signal-less" if best <= 5 else f"stale (newest={newest})"
        # Only render the most relevant pages (reports sub-pages first, then the IR
        # landing), capped — and in ONE browser session. Avoids per-page browser
        # launches and re-rendering every crawled page.
        pw_targets = (crawled[1:] + crawled[:1])[:PW_MAX_PAGES]
        print(f"    [escalate: {why}] Playwright render ({len(pw_targets)} page(s), local)")
        for u, (links, _t) in _pw_links_multi(pw_targets).items():
            print(f"    rendered {len(links)} links on {u[:70]}")
            for su, sa in links:
                add(su, sa)
        best, newest, stale = _assess()
        # Tier 2: Firecrawl stealth (paid) — only if still short, credits allowed, AND
        # within the time budget (it's the slow tier; don't blow the budget on it).
        if (best <= 5 or stale) and allow_fc and (time.time() - t0) < FETCH_BUDGET_SEC:
            why = "signal-less" if best <= 5 else f"stale (newest={newest})"
            print(f"    [escalate further: {why}] Firecrawl stealth ({len(pw_targets)} page(s))")
            for u in pw_targets:
                crawl_page(u, force_fc=True)
        elif (best <= 5 or stale) and allow_fc:
            print(f"    [skip Firecrawl tier: over {FETCH_BUDGET_SEC}s budget]")

    ranked = sorted(([sc, u, a] for u, (sc, a) in pdfs.items()), reverse=True)
    return ranked


def inspect_pdf(url: str, referer: str, save_path: str | None = None,
                force_save: bool = False) -> dict:
    """Download + open; report pages, text-layer, and SBC-term hits (Stage-1 stand-in).
    If save_path is given and the bytes are a real PDF, write them to disk."""
    data = b""
    try:
        import fc_client
        data = fc_client.fetch_pdf(url, referer=referer)
    except Exception as e1:
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Referer": referer}, timeout=TIMEOUT)
            data = r.content if r.content[:4] == b"%PDF" else b""
        except Exception as e2:
            return {"ok": False, "reason": f"download failed (bot-walled/opaque): {e2!r}"}
    if data[:4] != b"%PDF":
        return {"ok": False, "reason": "not a PDF (likely a viewer/stateful doc system)"}
    doc = fitz.open(stream=data, filetype="pdf")
    n = doc.page_count
    sample = " ".join(doc[i].get_text() for i in range(min(n, 40))).lower()
    hits = sorted({t for t in SBC_TERMS if t in sample})
    # CONTENT-based doc-type check (filename is useless on hash-named/tile IR sites):
    # read the cover and reject interim/quarterly announcements.
    cover = " ".join(doc[i].get_text() for i in range(min(n, 2))).lower()
    is_interim = bool(re.search(
        r"three months ended|six months ended|nine months ended|first quarter|"
        r"second quarter|third quarter|interim results|interim report|quarterly|"
        r"unaudited.{0,40}results|中期|季度|第[一二三]季", cover))
    is_annual = bool(re.search(
        r"annual report|annual financial|for the year ended|siemens report|"
        r"integrated report|年度報告|年報|年度报告|年报", cover))
    # fiscal year from the cover (the prominent year), for "pick the newest" logic
    cover_years = [int(y) for y in re.findall(r"(?<!\d)(?:20)\d{2}(?!\d)", cover)
                   if 2000 <= int(y) <= CURRENT_YEAR + 1]
    fiscal_year = max(cover_years) if cover_years else None
    # gate: a usable filing is LONG, mentions SBC, and is EITHER an annual report (10-K)
    # OR a RECENT (current/last-FY) quarterly/interim report. Per user: not restricted to
    # annual reports — a recent 2026 quarterly (10-Q) is acceptable too. An OLD interim
    # (older than last FY) is still rejected so we never surface stale data.
    recent = (fiscal_year or 0) >= CURRENT_YEAR - 1
    # FRESHNESS FLOOR: reject reports older than MIN_FISCAL_YEAR. Use the cover year;
    # if the cover year didn't parse, fall back to the year in the URL/filename. Only a
    # KNOWN year below the floor is rejected (an undatable doc isn't blocked on this rule).
    eff_year = fiscal_year or _year_in(unquote(urlparse(url).path))
    year_ok = (eff_year is None) or (eff_year >= MIN_FISCAL_YEAR)
    accept = (len(hits) >= 2 and n >= 40 and year_ok
              and ((not is_interim) or is_annual or recent))
    saved = None
    if save_path and (accept or force_save):  # persist a gate-passing doc (or when forced)
        with open(save_path, "wb") as f:
            f.write(data)
        saved = save_path
    if accept:
        note = ""
    elif n < 40:
        note = "too short (<40pp)"
    elif not year_ok:
        note = f"too old (FY{eff_year} < {MIN_FISCAL_YEAR})"
    elif is_interim and not is_annual and not recent:
        note = "interim/quarterly and not recent (older than last FY)"
    else:
        note = "insufficient SBC evidence"
    return {"ok": True, "pages": n, "bytes": len(data), "saved": saved,
            "text_layer": len(sample) > 500, "sbc_hits": hits,
            "is_interim": is_interim, "is_annual": is_annual, "fiscal_year": fiscal_year,
            "stage1_would_accept": accept, "gate_note": note}


def _registrable(host: str) -> str:
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def _search_candidate_pdfs(ir_url: str, name: str = "", limit: int = 8) -> list[str]:
    """Web-search FALLBACK for opaque IR platforms (Q4 Inc /static-files/<uuid>, JS-only
    listings) where the crawler's filename scorer finds no PDF. Search the issuer's OWN
    domain for the annual report and return URLs to probe by CONTENT (inspect_pdf decides).
    Stays within Diamond's scraper-only design: restricted to the issuer's own registrable
    domain — no SEC/EDGAR or third-party hosts."""
    reg = _registrable(urlparse(ir_url).netloc)
    if not reg:
        return []
    queries = []
    if name:
        queries.append(f"{name} annual report filetype:pdf")
    queries += [f"site:{reg} annual report pdf",
                f"site:{reg} annual report filetype:pdf"]
    try:
        from ddgs import DDGS
    except Exception:
        try:
            from duckduckgo_search import DDGS
        except Exception:
            print("    [search fallback unavailable: ddgs not installed]")
            return []
    urls, seen = [], set()
    for q in queries:
        try:
            with DDGS() as d:
                for r in d.text(q, max_results=limit):
                    u = r.get("href") or r.get("url") or ""
                    if not u or u in seen:
                        continue
                    if reg in urlparse(u).netloc.lower():   # issuer's own domain/CDN only
                        seen.add(u)
                        urls.append(u)
        except Exception as e:
            print(f"    [search fallback err: {e!r}]")
    return urls


def fetch_annual_report(ir_url: str, allow_fc: bool = True, save_path: str | None = None,
                        max_downloads: int = 8, name: str = "") -> dict | None:
    """Full flow: find candidates, download best-first, and SAVE the newest gate-passing
    annual report (not merely the first). Stops early once a current/last-FY report passes."""
    ranked = find_report_pdfs(ir_url, allow_fc)
    # probe NEWEST-YEAR first (not highest-score): old archive reports have richer anchors
    # and out-score recent ones, exhausting the budget before reaching the latest (Siemens).
    ranked = [r for r in ranked if r[0] > 0]
    ranked.sort(key=lambda r: (_pdf_year(r[1], r[2]), r[0]), reverse=True)
    passers, downloads = [], 0

    def _probe(u, label):
        nonlocal downloads
        info = inspect_pdf(u, ir_url)          # probe only; don't save yet
        downloads += 1
        tag = info.get("gate_note") or f"FY{info.get('fiscal_year')}"
        ok = bool(info.get("ok") and info.get("stage1_would_accept"))
        print(f"    probe{label} {('OK ' + str(info.get('pages')) + 'pp ' + tag) if ok else 'reject: ' + tag}  {u[:80]}")
        if ok:
            # rank key: newest FY, then prefer an annual report (10-K) over an interim
            # of the same year, then the longer document.
            passers.append((info.get("fiscal_year") or 0,
                            1 if info.get("is_annual") else 0,
                            info.get("pages") or 0, u, info))
        return ok

    for sc, u, a in ranked:
        if downloads >= max_downloads:
            break
        if _probe(u, f" [{sc:+.0f}]") and (passers[-1][0] >= CURRENT_YEAR - 1):
            break                              # current/last FY -> good enough

    # FALLBACK: opaque Q4/JS IR platforms (e.g. Tenaris /static-files/<uuid>) expose the
    # report at signal-less URLs the crawler can't score. Web-search the issuer's domain
    # and let the content gate pick the real annual report.
    if not passers:
        search_urls = _search_candidate_pdfs(ir_url, name)
        if search_urls:
            print(f"    [search fallback] probing {len(search_urls)} domain PDFs by content")
        for u in search_urls:
            if downloads >= max_downloads:
                break
            if _probe(u, "(search)") and (passers[-1][0] >= CURRENT_YEAR - 1):
                break

    if not passers:
        return None
    passers.sort(reverse=True)                 # newest FY, then annual>interim, then longer
    fy, _annual, _pages, u, info = passers[0]
    if save_path:
        info = inspect_pdf(u, ir_url, save_path=save_path)
    return {"url": u, "fiscal_year": fy, "info": info}


def fetch_reports(ir_url: str, allow_fc: bool = True, annual_path: str | None = None,
                  interim_path: str | None = None, max_downloads: int = 10,
                  name: str = "") -> dict:
    """Like fetch_annual_report, but captures BOTH the latest annual report AND the
    latest recent interim/quarterly report from the SAME single IR-page crawl. Used by
    the EU tab so that, if the annual report turns out to have no share-based-payment
    data, the already-downloaded interim can be tried instead — without a second scrape.

    The costly step (find_report_pdfs: crawl + render) runs ONCE; the only extra cost
    over fetch_annual_report is probing/saving the interim candidate, which usually sits
    on the same IR page. Saves the best annual to `annual_path` and the best recent
    interim to `interim_path` (either may be None if that kind wasn't found).

    Returns {"annual": {url,fiscal_year,info,path}|None,
             "interim": {url,fiscal_year,info,path}|None, "ir_url": ir_url}.
    """
    ranked_all = find_report_pdfs(ir_url, allow_fc)
    # Annual candidates: positive-scored (have an annual/FS doc-type signal).
    pos = [r for r in ranked_all if r[0] > 0]
    pos.sort(key=lambda r: (_pdf_year(r[1], r[2]), r[0]), reverse=True)
    # Interim candidates: the doc-type rubric scores interim/quarterly reports NEGATIVE,
    # so they sit below the >0 cut — pick them by NAME (any score) and let the content
    # gate (inspect_pdf: is_interim + recent) decide. They usually sit on the same IR page.
    intk = [r for r in ranked_all
            if re.search(_INTERIM_NAME_RE, (str(r[2]) + " " + str(r[1])).lower())]
    intk.sort(key=lambda r: (_pdf_year(r[1], r[2]), r[0]), reverse=True)

    passers, downloads, seen, probed = [], 0, set(), {}
    recent = CURRENT_YEAR - 1

    def _passes_interim(info: dict) -> bool:
        # Relaxed gate for interim / half-year FINANCIAL reports: they are legitimately
        # shorter than an annual (the strict >=40pp floor would always reject them), but
        # must still be a recent, substantial document carrying a share-based-payment
        # term — so short earnings press releases are excluded.
        return (bool(info.get("ok"))
                and (info.get("fiscal_year") or 0) >= recent
                and (info.get("pages") or 0) >= 20
                and len(info.get("sbc_hits") or []) >= 1)

    def _probe(u, label):
        nonlocal downloads
        if u in seen:
            return False
        seen.add(u)
        info = inspect_pdf(u, ir_url)          # probe only; save the winners afterward
        probed[u] = info
        downloads += 1
        ok = bool(info.get("ok") and info.get("stage1_would_accept"))
        tag = info.get("gate_note") or f"FY{info.get('fiscal_year')}"
        print(f"    probe{label} {('OK ' + str(info.get('pages')) + 'pp ' + tag) if ok else 'reject: ' + tag}  {u[:80]}")
        if ok:
            passers.append((info.get("fiscal_year") or 0,
                            1 if info.get("is_annual") else 0,
                            info.get("pages") or 0, u, info))
        return ok

    # 1) Annual: probe positive candidates newest-first, stop once a recent annual lands
    #    (plus a couple extra in case the interim is also positive-scored).
    extra_after_annual = 0
    for sc, u, a in pos:
        if downloads >= max_downloads:
            break
        _probe(u, f" [{sc:+.0f}]")
        if any(p[1] == 1 and p[0] >= recent for p in passers):
            if any(p[1] == 0 and p[0] >= recent for p in passers):
                break                          # already have a recent annual + interim
            extra_after_annual += 1
            if extra_after_annual >= 2:
                break

    # 2) Interim: if no interim passed yet, evaluate the interim-named candidates with
    #    the RELAXED gate. Reuse any already-probed info (no re-download); otherwise probe.
    if not any(p[1] == 0 for p in passers):
        interim_probes = 0
        for sc, u, a in intk:
            if interim_probes >= 3:
                break
            info = probed.get(u)
            if info is None:
                if downloads >= max_downloads:
                    break
                seen.add(u)
                info = inspect_pdf(u, ir_url)
                probed[u] = info
                downloads += 1
                interim_probes += 1
            ok = _passes_interim(info)
            print(f"    interim [{sc:+.0f}] "
                  f"{('OK ' + str(info.get('pages')) + 'pp FY' + str(info.get('fiscal_year'))) if ok else 'reject: ' + str(info.get('pages')) + 'pp FY' + str(info.get('fiscal_year')) + ' sbc=' + str(len(info.get('sbc_hits') or []))}  {u[:70]}")
            if ok:
                passers.append((info.get("fiscal_year") or 0, 0,
                                info.get("pages") or 0, u, info))
                if (info.get("fiscal_year") or 0) >= recent:
                    break

    # FALLBACK: opaque Q4/JS IR platforms expose nothing scoreable — same web-search
    # leg as fetch_annual_report (only when the crawl yielded no passer at all).
    if not passers:
        for u in _search_candidate_pdfs(ir_url, name):
            if downloads >= max_downloads:
                break
            if _probe(u, "(search)") and passers[-1][0] >= recent:
                break

    annuals = sorted([p for p in passers if p[1] == 1], reverse=True)
    interims = sorted([p for p in passers if p[1] == 0], reverse=True)

    out: dict = {"annual": None, "interim": None, "ir_url": ir_url}
    if annuals and annual_path:
        fy, _a, _p, u, info = annuals[0]
        info = inspect_pdf(u, ir_url, save_path=annual_path)
        out["annual"] = {"url": u, "fiscal_year": fy, "info": info, "path": annual_path}
    if interims and interim_path:
        fy, _a, _p, u, info = interims[0]
        # force_save: an interim passes the RELAXED gate, not the strict one inspect_pdf
        # enforces for save — so persist it explicitly.
        info = inspect_pdf(u, ir_url, save_path=interim_path, force_save=True)
        out["interim"] = {"url": u, "fiscal_year": fy, "info": info, "path": interim_path}
    return out


# ---------------------------------------------------------------- demo
if __name__ == "__main__":
    allow_fc = "--firecrawl" in sys.argv
    # (ir_url, name) pairs from the resolver output
    TARGETS = [
        ("https://www.shell.com/investors.html", "Shell plc"),
        ("https://www.dollarama.com/en-CA/corp/investor-relations", "Dollarama Inc"),
    ]
    for ir_url, name in TARGETS:
        print(f"\n{'='*70}\n{name}\n  IR page: {ir_url}")
        ranked = find_report_pdfs(ir_url, allow_fc)
        if not ranked:
            print("  no PDF candidates found (try --firecrawl for JS pages)")
            continue
        print("  top candidates:")
        for sc, u, a in ranked[:5]:
            print(f"    [{sc:+.0f}] {a[:50]!r}  {u[:90]}")
        best_sc, best_url, _ = ranked[0]
        if best_sc <= 0:
            print("  best candidate scored <=0 -> ABSTAIN (no clear annual report)")
            continue
        print(f"  downloading top: {best_url}")
        info = inspect_pdf(best_url, ir_url)
        print(f"  inspect: {info}")
