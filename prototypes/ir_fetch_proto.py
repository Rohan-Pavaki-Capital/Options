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
import re, sys, io, time, unicodedata
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
# Hard freshness floor: reject any report older than this fiscal year (per request
# 2026-07-09 — "for annual report only use 2025 annual report"; supersedes the earlier
# "max old year should be 2024" rule). At CURRENT_YEAR=2026 that's 2025.
MIN_FISCAL_YEAR = CURRENT_YEAR - 1

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
                    # Indonesian: "Laporan Triwulan" (quarterly report) — BCA
                    # links its quarterly financial reports that way.
                    r"triwulan|kuartal|"
                    r"半年|中期|季度|季報|季报")

# quarterly-cadence financial-RESULTS documents (JP tanshin "(Consolidated)
# Summary Report", "Financial Results", "Financial Highlights" decks) — used
# by fetch_reports(purpose="balance_sheet") to pick the freshest results doc
# carrying a balance sheet. Like the interim leg they are picked by NAME (the
# doc-type rubric scores them negative); the content gate is BS-oriented
# (recent + a real balance-sheet page), NOT SBC-oriented — results docs carry
# no share-based-payment note. Statement-class names (tanshin/summary/results
# — full consolidated statements) outrank deck-class names (highlights /
# chart-slide summaries of the same period): MUFG's summary2603_en.pdf (63pp,
# real Consolidated Balance Sheets) beats highlights2603_en.pdf (24pp slide).
_RESULTS_STATEMENT_RE = (r"summary report|financial results|business results|"
                         r"results announcement|earnings report|tanshin|"
                         r"決算短信|短信|決算")
_RESULTS_NAME_RE = (_RESULTS_STATEMENT_RE +
                    r"|financial highlights|\bhighlights?\b|financial summary")
# deck-word demotion inside the results ranking: a "Financial Results
# Presentation" is a slide deck even though it name-matches the statement class.
# Pillar-3 / risk-exposure & capital reports (BCA: "laporan eksposur risiko dan
# permodalan" sits beside each quarterly) are regulatory capital disclosures,
# never the statement source — demoted the same way.
_RESULTS_DECK_RE = (r"presentation|slides?\b|deck|databook|highlight|"
                    r"pillar ?3|risk exposure|eksposur|basel")

# share-based-payment terms (subset of keywords.py) — acceptance probe (EN + CJK)
SBC_TERMS = [
    "share-based payment", "share based payment", "stock option", "share option",
    "stock-based compensation", "equity-settled", "options outstanding",
    "exercise price", "vesting", "restricted stock", "rsu", "esop", "grant date",
    # Chinese (Traditional/Simplified): share-based payment / equity incentive / option / vesting
    "股份支付", "股權激勵", "股权激励", "以股份為基礎", "購股權", "认股权", "認股權",
    "限制性股票", "受限制股份", "期權", "归属", "歸屬",
    # French (official-language IR PDFs, e.g. Eiffage's URD): share-based
    # payment / stock options / free-share plans. Apostrophe-free spellings —
    # French PDFs mix ' and ’ so terms with apostrophes would miss.
    "fondés sur des actions", "fondé sur des actions",
    "options de souscription", "actions gratuites", "attributions gratuites",
    "actions de performance",
]

# Balance-sheet statement presence probe — a genuine filing carries a page
# titled like the statement (contiguous title) WITH an assets total on it or the
# next page; results-presentation decks don't (Kawasaki's deck captions a slide
# "–Statement of Financial Position-" but never matches the contiguous
# "Consolidated Statement of ..." variants and so fails Stage 1 downstream).
# Checking CONTENT here means a mis-scored deck is rejected at the probe instead
# of failing the pipeline later. Mirrors Balance_sheet Stage 1's title list.
try:
    from Balance_sheet.config import TITLE_VARIANTS as _BS_TITLE_VARIANTS
except Exception:  # standalone run without the Balance_sheet package on path
    _BS_TITLE_VARIANTS = [
        "CONDENSED CONSOLIDATED BALANCE SHEET", "CONSOLIDATED BALANCE SHEET",
        "BALANCE SHEET", "STATEMENTS OF FINANCIAL POSITION",
        "CONSOLIDATED STATEMENTS OF FINANCIAL POSITION",
        "CONSOLIDATED STATEMENT OF FINANCIAL POSITION",
        "BILAN CONSOLIDÉ", "ÉTAT CONSOLIDÉ DE LA SITUATION FINANCIÈRE",
        "ÉTAT DE LA SITUATION FINANCIÈRE CONSOLIDÉE", "COMPTES CONSOLIDÉS",
    ]
# CJK statement titles + assets-total markers (JP/CN/HK-language filings), so
# the check never rejects a legitimate CJK report the SBC gate accepts.
_BS_TITLES_CJK = ["資產負債表", "资产负债表", "財務狀況表", "财务状况表",
                  "貸借対照表", "財政状態計算書"]
_BS_ASSETS_MARKERS = [
    "total assets", "total current assets", "total de l'actif", "total actif",
    "資產總額", "资产总额", "總資產", "总资产", "資產合計", "资产合计", "資産合計",
]
# Junk-page rejection — mirrors Stage 1 exactly. Multi-year summaries
# (MUFG's integrated report prints "Balance sheet data:" + "Total assets" on
# its Ten-Year Summary page and carries NO real statement anywhere) and
# non-title wording ("Balance sheet data:", "Average balance sheets",
# "off-balance sheet") must not make a document pass the probe.
try:
    from Balance_sheet.pdf_locator import (_looks_like_multiyear_summary,
                                           _title_match)
except Exception:  # standalone run without the Balance_sheet package on path
    _YEAR_TOKEN_RE = re.compile(r"\b(?:fy\s?)?(20\d{2})\b")

    def _looks_like_multiyear_summary(text: str) -> bool:
        years = sorted({int(m.group(1))
                        for m in _YEAR_TOKEN_RE.finditer(text)})
        best = run = 1 if years else 0
        for prev, cur in zip(years, years[1:]):
            run = run + 1 if cur == prev + 1 else 1
            best = max(best, run)
        return best >= 5

    def _title_match(text: str, variants: list) -> "str | None":
        for v in variants:
            for m in re.finditer(re.escape(v), text):
                tail = text[m.end():m.end() + 10].lstrip(" \n:—-*")
                if tail.startswith(("data", "date")):
                    continue
                head = text[max(0, m.start() - 12):m.start()].rstrip(" \n-")
                if head.endswith(("average", "off")):
                    continue
                return v
        return None


def _fold_text(text: str) -> str:
    """Lowercase + strip accents + normalize apostrophes — mirrors the Stage-1
    locator's folding so FR titles match here the same way they will there."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.replace("’", "'").lower()


def _has_balance_sheet_page(doc: "fitz.Document") -> bool:
    """True when some page carries a balance-sheet / financial-position TITLE
    and that page (or the next — two-page statements) carries an assets total.
    Approximates what Stage 1 (pdf_locator.locate_balance_sheet) will accept."""
    variants = [_fold_text(v) for v in _BS_TITLE_VARIANTS] + _BS_TITLES_CJK
    n = doc.page_count
    for i in range(n):
        text = _fold_text(doc[i].get_text())
        if _title_match(text, variants) is None:
            continue
        if _looks_like_multiyear_summary(text):
            continue  # summary table, not the statement (Stage 1 skips it too)
        window = text + ("\n" + _fold_text(doc[i + 1].get_text())
                         if i + 1 < n else "")
        if any(m in window for m in _BS_ASSETS_MARKERS):
            return True
    return False


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
    # interim — incl. compact forms Q3FY26 / FY26Q3 / 3Q26, Indonesian "triwulan"
    (r"interim|half-?year|\bquarter|first quarter|third quarter|6 months|"
     r"\bq[1-4]\b|q[1-4]\s*fy|fy\s*\d{2}\s*q[1-4]|[1-4]q\d{2}|q[1-4]\d{2}|q[1-4]fy|"
     r"triwulan|kuartal", 45),
    (r"\bmd&?a\b|management discussion", 25),                 # MD&A alone is not the statements
    (r"tender|offer to purchase|prospectus|supplement", 40),
    (r"esg|sustainab|\bcsr\b|climate|carbon|diversity|impact report", 45),
    (r"proxy|circular|\bagm\b|notice of meeting|information statement|voting", 30),
    (r"present|slides|transcript|webcast|fact ?sheet|infographic|fireside", 35),
    # presentation-material filename prefix (Kawasaki "pre_260512-1e.pdf") — the
    # anchor may carry no "presentation" wording when the deck is linked from a
    # listing page, so catch the filename convention too.
    (r"/pre[-_]\d", 35),
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
    # hub-and-spoke IR libraries (Kawasaki): the statements sit behind
    # "Financial Results" / "Performance / Financial Information" / "IR Library"
    # sub-pages whose anchors carried no signal under the patterns above.
    (r"financial[- ]?(?:results?|information)", 24),
    (r"ir[- ]?library|\blibrary\b", 14),
    (r"financials\b", 20), (r"\bfilings?\b", 16), (r"reports?\b", 10), (r"investor", 6),
    # Indonesian IR sections (BCA: "Hubungan Investor" hub with "Laporan
    # Keuangan" / "Laporan Tahunan" listing pages) — without these the crawl
    # never follows past the homepage and the report PDFs stay invisible.
    (r"laporan[- _]?keuangan", 30), (r"laporan[- _]?tahunan", 26),
    (r"\blaporan\b", 10), (r"hubungan[- _]?investor", 6),
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
    from core import fc_client
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


def _url_stamp(u: str, anchor: str = "") -> int:
    """YYYYMMDD upload stamp in the path/anchor (CMS filename prefixes like
    "20260423-financial-report-march-2026.pdf") — a sharper recency key than
    the bare year, which ties a March-2026 quarterly with the December-2025
    one sitting in the same /2026/ folder. 0 when absent."""
    blob = unquote(urlparse(u).path) + " " + anchor
    stamps = [int(s) for s in
              re.findall(r"(?<!\d)(20\d{2}[01]\d[0-3]\d)(?!\d)", blob)]
    return max(stamps, default=0)


def _quarterly_rank(u: str, anchor: str = ""):
    """Sort key (ascending) for quarterly-candidate probing in the
    balance-sheet search fallback: results/interim-NAMED non-deck docs first,
    then newest by filename date stamp, then by year."""
    blob = (anchor + " " + unquote(urlparse(u).path)).lower()
    named = bool(re.search(_RESULTS_NAME_RE + "|" + _INTERIM_NAME_RE, blob))
    deck = bool(re.search(_RESULTS_DECK_RE, blob))
    tier = 0 if (named and not deck) else (1 if named else 2)
    return (tier, -_url_stamp(u, anchor), -(_year_in(blob) or 0))


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
    # GATE: strong NEGATIVE doc-type evidence over a weak positive (a results
    # PRESENTATION whose only positive hit is an FY token) can't be rescued by
    # the recency/English bonuses either — Kawasaki's FY2025 slide deck scored
    # +6 that way (8 pos - 35 present + 8 en + 25 recency) and won the probe.
    if neg > pos and pos < 20:
        s = min(s, 0)
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

    def _page_candidates(page_links):
        cands = []
        for u, a in page_links:
            blob = f"{a} {urlparse(u).path}".lower()
            ps = sum(w for pat, w in REPORTS_PAGE if re.search(pat, blob))
            ps -= sum(w for pat, w in REPORTS_PAGE_NEG if re.search(pat, blob))
            if ps > 0 and urlparse(u).netloc and not re.search(r"\.pdf", u, re.I):
                cands.append((ps, u, a))
        cands.sort(reverse=True)
        return cands

    crawled = [ir_url]
    links = crawl_page(ir_url)

    # follow the best "reports / financial statements" sub-pages
    seen = {ir_url}
    level2_links = []
    for ps, u, a in _page_candidates(links)[:3]:
        if u in seen:
            continue
        seen.add(u)
        crawled.append(u)
        print(f"    -> follow reports page (score {ps}): {u}")
        level2_links += crawl_page(u)

    # ONE more hop for hub-and-spoke IR libraries (Kawasaki: IR landing ->
    # "IR Library" -> "Financial Results"): the level-1 pages carry no report
    # PDFs at all, only better-named sub-pages. Follow the strongest NEW
    # sub-pages found on them; the >=12 floor skips weak "investor"-grade links.
    for ps, u, a in [c for c in _page_candidates(level2_links) if c[0] >= 12][:2]:
        if u in seen:
            continue
        seen.add(u)
        crawled.append(u)
        print(f"    -> follow reports sub-page (score {ps}): {u}")
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


def _download_pdf(url: str, referer: str) -> tuple[bytes, str]:
    """One download attempt (Firecrawl PDF fetch, then plain requests).
    Returns (bytes, "") on a real PDF, (b"", reason) otherwise."""
    try:
        from core import fc_client
        data = fc_client.fetch_pdf(url, referer=referer)
        if data[:4] == b"%PDF":
            return data, ""
    except Exception:
        pass
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Referer": referer},
                         timeout=TIMEOUT)
        if r.content[:4] == b"%PDF":
            return r.content, ""
        return b"", (f"not a PDF (HTTP {r.status_code}; likely a "
                     f"viewer/stateful doc system)")
    except Exception as e:
        return b"", f"download failed (bot-walled/opaque): {e!r}"


def inspect_pdf(url: str, referer: str, save_path: str | None = None,
                force_save: bool = False, bs_probe: bool = False) -> dict:
    """Download + open; report pages, text-layer, and SBC-term hits (Stage-1 stand-in).
    If save_path is given and the bytes are a real PDF, write them to disk.
    `bs_probe`: balance-sheet-purpose probing — compute `has_balance_sheet`
    even for short docs with zero SBC hits (JP tanshin / financial-highlights
    results docs never mention share-based payments; some tanshin run <20pp)."""
    data, why = _download_pdf(url, referer)
    if not data and "+" in urlparse(url).path:
        # Jahia/AEM-style CMSes store files with literal spaces (%20); some
        # crawl/search tiers return the links '+'-encoded, which these servers
        # 404 (e.g. eiffage.com "Rapport+Annuel/..."). Retry once with %20.
        data, why2 = _download_pdf(url.replace("+", "%20"), referer)
        if not data:
            why = why2
    if not data:
        return {"ok": False, "reason": why}
    doc = fitz.open(stream=data, filetype="pdf")
    n = doc.page_count
    # SBC sampling: the first 40 pages cover a US 10-K, but European URDs run
    # 400+ pages with the financial statements (and the share-based-payment
    # note) deep in the document — stride-sample the remainder so they count.
    sample_pages = list(range(min(n, 40)))
    if n > 100:
        sample_pages += list(range(40, n, 5))
    sample = " ".join(doc[i].get_text() for i in sample_pages).lower()
    hits = sorted({t for t in SBC_TERMS if t in sample})
    # CONTENT-based doc-type check (filename is useless on hash-named/tile IR sites):
    # read the cover and reject interim/quarterly announcements.
    cover = " ".join(doc[i].get_text() for i in range(min(n, 2))).lower()
    is_interim = bool(re.search(
        r"three months ended|six months ended|nine months ended|first quarter|"
        r"second quarter|third quarter|interim results|interim report|quarterly|"
        r"unaudited.{0,40}results|中期|季度|第[一二三]季", cover))
    # "for the (fiscal) year(s) ended": audited multi-year statements title the
    # cover "For the Years ended March 31, 2026 and 2025" (Kawasaki) and JP
    # tanshin covers say "for the Fiscal Year Ended" — both are annual filings.
    is_annual = bool(re.search(
        r"annual report|annual financial|for the (?:fiscal )?years? ended|siemens report|"
        r"integrated report|年度報告|年報|年度报告|年报|"
        # French: "Document d'enregistrement universel" (URD, apostrophe-free
        # match), "Rapport (financier) annuel", "exercice clos le ..." —
        # \s+ because cover titles wrap across lines ("d'enregistrement \n
        # universel").
        r"enregistrement\s+universel|rapport\s+annuel|"
        r"rapport\s+financier\s+annuel|exercice\s+clos", cover))
    # fiscal year from the cover: prefer the year printed WITH the report
    # title ("Document d'enregistrement universel 2025", "Annual Report
    # 2025") — covers also carry unrelated later dates (AGM, filing date)
    # that would win a bare max(); fall back to the prominent max year.
    cover_years = [int(y) for y in re.findall(r"(?<!\d)(?:20)\d{2}(?!\d)", cover)
                   if 2000 <= int(y) <= CURRENT_YEAR + 1]
    m_title_year = re.search(
        r"(?:annual\s+report|registration\s+document|enregistrement\s+universel|"
        r"rapport\s+annuel|rapport\s+financier\s+annuel)[^\d]{0,15}((?:20)\d{2})",
        cover)
    if m_title_year and 2000 <= int(m_title_year.group(1)) <= CURRENT_YEAR + 1:
        fiscal_year = int(m_title_year.group(1))
    else:
        fiscal_year = max(cover_years) if cover_years else None
    if fiscal_year is None:
        # Cover text unusable — CID-font covers can decode digits to control
        # chars (Kawasaki's audited CFS prints 2026 as "202\x19") — so fall
        # back to years printed next to a reporting-period phrase in the
        # sampled body text, where the statement pages' fonts decode cleanly.
        # Anchoring to the phrase keeps unrelated years (bond maturities,
        # subsequent-event dates) from inflating the fiscal year.
        # non-greedy any-char gap: the day number sits between the phrase and
        # the year ("as of march 31, 2026"), so a digit-free gap never reaches it.
        period_years = [int(y) for y in re.findall(
            r"(?:years?\s+end(?:ed|ing)|as\s+(?:of|at))"
            r"[\s\S]{0,40}?(?<!\d)((?:20)\d{2})(?!\d)",
            sample) if 2000 <= int(y) <= CURRENT_YEAR + 1]
        fiscal_year = max(period_years) if period_years else None
    bs_fresh = None
    if bs_probe:
        # Results docs (JP tanshin) print NEXT-year forecast dates on the
        # cover ("Dividends for the fiscal year ENDING March 31, 2027") that
        # inflate the bare max-year read — prefer the reporting-period year
        # ("for the fiscal year/three months ENDED March 31, 2026"). "ended"
        # only, so forecast "ending" phrases never match. bs_probe-scoped:
        # options-path probing is untouched.
        ended_years = [int(y) for y in re.findall(
            r"ended[\s\S]{0,40}?(?<!\d)((?:20)\d{2})(?!\d)", cover)
            if 2000 <= int(y) <= CURRENT_YEAR + 1]
        if ended_years:
            fiscal_year = max(ended_years)
        # FRESHNESS: a quarterly-cadence doc is only useful if it's newer than
        # what the annual would carry — Sanofi's "Half-year financial report
        # 2025" (period Jun 30 2025) passed the bare FY floor and DISPLACED
        # the FY2025 annual (period Dec 31 2025), serving 6-months-staler
        # data. Latest full date on the cover (period end or publication —
        # both track recency; "March 31, 2026" and "30 June 2025" orders)
        # must be within ~9 months; anything older is superseded by a fresher
        # results doc or the annual. None (no parseable date) → caller falls
        # back to a strict current-year floor.
        import datetime as _dt
        _MONTHS = {m: i + 1 for i, m in enumerate(
            ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"])}
        _mon_re = "|".join(_MONTHS)
        _dates = [(int(y), _MONTHS[mo], int(d)) for mo, d, y in re.findall(
            rf"({_mon_re})\s+(\d{{1,2}}),?\s+((?:20)\d{{2}})", cover)]
        _dates += [(int(y), _MONTHS[mo], int(d)) for d, mo, y in re.findall(
            rf"(\d{{1,2}})\s+({_mon_re})\s+((?:20)\d{{2}})", cover)]
        _dates = [d for d in _dates
                  if 2000 <= d[0] <= CURRENT_YEAR + 1 and 1 <= d[2] <= 31]
        if _dates:
            y, m, d = max(_dates)
            bs_fresh = ((_dt.date.today() - _dt.date(y, m, min(d, 28))).days
                        <= 280)
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
    # CONTENT doc-type check: a genuine filing has a balance-sheet /
    # financial-position statement page; presentation decks and summary docs
    # don't. Only computed once the cheap gates pass (it reads every page);
    # the >=1-hit / >=20pp floor matches the relaxed interim gate's minimums
    # so _passes_interim always sees a computed value, not None.
    has_bs = None
    if len(hits) >= 1 and n >= 20 and year_ok:
        has_bs = _has_balance_sheet_page(doc)
    elif bs_probe and n >= 6 and year_ok:
        # balance-sheet purpose: the SBC/pages preconditions above never hold
        # for results docs — compute the (only) check that matters for them.
        has_bs = _has_balance_sheet_page(doc)
    accept = (len(hits) >= 2 and n >= 40 and year_ok and bool(has_bs)
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
    elif has_bs is False:
        note = "no balance-sheet statement page (presentation/summary doc?)"
    elif is_interim and not is_annual and not recent:
        note = "interim/quarterly and not recent (older than last FY)"
    else:
        note = "insufficient SBC evidence"
    return {"ok": True, "pages": n, "bytes": len(data), "saved": saved,
            "text_layer": len(sample) > 500, "sbc_hits": hits,
            "is_interim": is_interim, "is_annual": is_annual, "fiscal_year": fiscal_year,
            "has_balance_sheet": has_bs, "bs_fresh": bs_fresh,
            "stage1_would_accept": accept, "gate_note": note}


# Public second-level suffixes under 2-letter ccTLDs (co.jp, co.uk, com.br,
# co.kr, com.hk, ...): the registrable name there is THREE labels — naively
# taking two returns the bare suffix ("co.jp"), and the search fallback's
# own-domain filter then matches EVERY company on that suffix (Kawasaki's
# crawl accepted a kagome.co.jp PDF as its annual report).
_CC_SLD = {"co", "com", "ne", "net", "or", "org", "ac", "go", "gov", "edu"}


def _registrable(host: str) -> str:
    parts = host.lower().split(".")
    if len(parts) >= 3 and parts[-2] in _CC_SLD and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def _search_candidate_pdfs(ir_url: str, name: str = "", limit: int = 8,
                           kind: str = "annual") -> list[str]:
    """Web-search FALLBACK for opaque IR platforms (Q4 Inc /static-files/<uuid>, JS-only
    listings) where the crawler's filename scorer finds no PDF. Search the issuer's OWN
    domain for the report and return URLs to probe by CONTENT (inspect_pdf decides).
    Stays within Diamond's scraper-only design: restricted to the issuer's own registrable
    domain — no SEC/EDGAR or third-party hosts.

    `kind="quarterly"`: quarterly-oriented queries for the balance-sheet flow's
    quarterly-first rule — the annual queries can never surface a quarterly
    (BCA: the crawl was blind to the JS/Indonesian IR pages and the annual-only
    search then served a year-stale balance sheet). Includes an Indonesian
    "laporan keuangan triwulan" query; on non-Indonesian domains it just
    returns nothing."""
    reg = _registrable(urlparse(ir_url).netloc)
    if not reg:
        return []
    queries = []
    if kind == "quarterly":
        if name:
            queries.append(f"{name} quarterly financial report filetype:pdf")
        queries += [f"site:{reg} quarterly report pdf",
                    f"site:{reg} interim financial statements pdf",
                    f"site:{reg} laporan keuangan triwulan pdf"]
    else:
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
                    _h = urlparse(u).netloc.lower()
                    if _h == reg or _h.endswith("." + reg):   # issuer's own domain/CDN only
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
        # gate_note (gated), then reason (download/non-PDF failure — without
        # this those probes all print an opaque "FYNone"), then the FY tag.
        tag = (info.get("gate_note") or info.get("reason")
               or f"FY{info.get('fiscal_year')}")
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
                  name: str = "", early_sink: dict | None = None,
                  purpose: str = "options",
                  results_path: str | None = None) -> dict:
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

    `early_sink`: callers run this whole function under a wall-clock cap
    (backend Stage-0: 100s), and the crawl alone can cost 60-90s — so the
    annual is SAVED the moment it passes the gate (exactly where
    fetch_annual_report stops), and recorded into `early_sink["annual"]`.
    The interim probing that follows is a bonus; if the caller's cap expires
    during it, the caller can salvage the already-saved annual from the sink
    instead of discarding a successful fetch (Kawasaki: the 73pp annual
    passed, then interim probes blew the budget).

    `purpose="balance_sheet"` + `results_path`: quarterly-first mode for the
    balance-sheet flow (user rule 2026-07-16: quarterly report first, then
    annual). The quarterly-cadence financial-results docs (_RESULTS_NAME_RE)
    are probed FIRST with a BS-content gate (recent + real balance-sheet
    page — no SBC requirement); the first passer is saved to `results_path`,
    returned under out["results"] (and early_sink["results"]), and the
    annual/interim probing is SKIPPED — the caller keeps its own
    annual/EDGAR/EDINET fallback chain. When the CRAWL surfaces no passing
    results doc, a quarterly-oriented own-domain web search
    (_search_candidate_pdfs kind="quarterly") is probed with the same gate
    first; only when that also fails does the flow fall through to the
    normal annual-first behavior unchanged.
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
    early_annual: dict | None = None

    # 0) BALANCE-SHEET purpose: probe quarterly-cadence results docs first
    #    (see docstring). Statement-class names (tanshin/summary/results)
    #    outrank deck-class (highlights/presentation) so a full consolidated
    #    balance sheet beats a chart-slide summary of the same period; the
    #    _has_balance_sheet_page gate rejects decks whose "summary slide"
    #    carries no real statement title (Kawasaki-style presentations).
    if purpose == "balance_sheet" and results_path:
        # Candidates: results-named (JP tanshin/highlights) PLUS interim-named
        # (EU quarterlies are anchored "Q1 2026", "half-year report", "press
        # release ... first-quarter results" — no "financial results" wording).
        # The BS-content gate below decides; a news press release without a
        # balance sheet is rejected in one cheap probe.
        res_cands = [r for r in ranked_all
                     if re.search(_RESULTS_NAME_RE + "|" + _INTERIM_NAME_RE,
                                  (str(r[2]) + " " + str(r[1])).lower())]

        def _res_rank(r):
            blob = (str(r[2]) + " " + str(r[1])).lower()
            tier = (2 if re.search(_RESULTS_DECK_RE, blob)
                    else 0 if re.search(_RESULTS_STATEMENT_RE, blob) else 1)
            # filename date stamp before bare year: a March-2026 quarterly and
            # the December-2025 one share the same /2026/ folder (= same year).
            return (tier, -_url_stamp(r[1], str(r[2])),
                    -(_pdf_year(r[1], r[2]) or 0), -r[0])

        res_cands.sort(key=_res_rank)

        def _try_results(u, sc, label=""):
            """Probe ONE results/quarterly candidate with the BS content gate;
            returns the saved result dict when it passes, else None."""
            nonlocal downloads
            if u in seen:
                return None
            seen.add(u)
            info = inspect_pdf(u, ir_url, bs_probe=True)
            probed[u] = info
            downloads += 1
            fresh = (info.get("bs_fresh") is True
                     # no parseable cover date: strict current-year floor
                     # (a bare FY >= last-year floor let Sanofi's H1-2025
                     # report displace the fresher FY2025 annual)
                     or (info.get("bs_fresh") is None
                         and (info.get("fiscal_year") or 0) >= CURRENT_YEAR))
            ok = (bool(info.get("ok"))
                  and (info.get("fiscal_year") or 0) >= recent
                  and (info.get("pages") or 0) >= 6
                  and bool(info.get("has_balance_sheet"))
                  and fresh)
            tag = (f"OK {info.get('pages')}pp FY{info.get('fiscal_year')}"
                   if ok else
                   "reject: " + ("stale period (superseded results doc)"
                                 if (bool(info.get("ok"))
                                     and info.get("has_balance_sheet")
                                     and not fresh)
                                 else str(info.get("gate_note")
                                          or info.get("reason") or "?")))
            print(f"    results{label} [{sc:+.0f}] {tag}  {u[:70]}")
            if not ok:
                return None
            # re-download with save: results docs never pass inspect_pdf's
            # strict save gate, so persist explicitly (like the interim leg).
            info = inspect_pdf(u, ir_url, save_path=results_path,
                               force_save=True, bs_probe=True)
            return {"url": u, "fiscal_year": info.get("fiscal_year"),
                    "info": info, "path": results_path}

        res = None
        res_probes = 0
        for sc, u, a in res_cands:
            if res_probes >= 5 or downloads >= max_downloads:
                break
            before = downloads
            res = _try_results(u, sc)
            res_probes += downloads - before
            if res:
                break
        # SEARCH FALLBACK (quarterly): the crawl surfaced no acceptable
        # results doc — search the issuer's own domain for quarterly/interim
        # reports and probe by content with the SAME gate. Without this the
        # quarterly-first rule silently degrades to annual whenever the IR
        # listing is JS-rendered or non-English (BCA: "Laporan Triwulan"
        # pages invisible to the crawl; the annual-only search fallback then
        # served a year-stale balance sheet).
        if res is None and downloads < max_downloads:
            q_urls = _search_candidate_pdfs(ir_url, name, kind="quarterly")
            # Direct PDF hits, PLUS PDFs harvested from LISTING-page hits: the
            # search often lands on the quarterly listing page rather than the
            # PDF itself (BCA's "Laporan Keuangan" page holds the March-2026
            # report none of the queries surface directly) — harvesting it is
            # one cheap fetch. Only results/interim-NAMED PDF links are kept
            # from a listing (BCA's page also lists ~60 monthly/ESG docs).
            cands = [(u, "") for u in q_urls
                     if re.search(r"\.pdf(\?|$)", u, re.I)]
            pages = [u for u in q_urls
                     if not re.search(r"\.pdf(\?|$)", u, re.I)
                     and re.search(r"laporan|keuangan|report|financial|"
                                   r"investor|quarterly|interim|results",
                                   u, re.I)]
            for pu in pages[:2]:
                try:
                    plinks, _t, via = get_links(pu, allow_fc)
                except Exception:
                    continue
                named = [(su, sa) for su, sa in plinks
                         if re.search(r"\.pdf(\?|$)", su, re.I)
                         and re.search(
                             _RESULTS_NAME_RE + "|" + _INTERIM_NAME_RE,
                             (sa + " " + unquote(urlparse(su).path)).lower())]
                print(f"    [search fallback] {len(named)} quarterly-named "
                      f"PDFs via {via} on listing page {pu[:60]}")
                cands += named
            uniq, seen_u = [], set()
            for u, a in cands:
                if u not in seen_u:
                    seen_u.add(u)
                    uniq.append((u, a))
            uniq.sort(key=lambda c: _quarterly_rank(c[0], c[1]))
            if uniq:
                print(f"    [search fallback] probing up to 5 of "
                      f"{len(uniq)} quarterly candidates (BS gate)")
            sq_probes = 0
            for u, a in uniq:
                if sq_probes >= 5 or downloads >= max_downloads:
                    break
                before = downloads
                res = _try_results(u, 0, "(search)")
                sq_probes += downloads - before
                if res:
                    break
        if res is not None:
            if early_sink is not None:
                early_sink["results"] = res
            return {"annual": None, "interim": None, "results": res,
                    "ir_url": ir_url}

    def _secure_annual():
        # Save the newest RECENT annual as soon as one has passed the gate (see
        # docstring). The early save is authoritative — never rewritten later —
        # so a caller that salvages the file after its cap expired cannot race
        # the still-running orphan thread rewriting it.
        nonlocal early_annual
        if early_annual is not None or not annual_path:
            return
        ra = sorted([p for p in passers if p[1] == 1 and p[0] >= recent],
                    reverse=True)
        if not ra:
            return
        fy, _a, _p, u, _info = ra[0]
        info = inspect_pdf(u, ir_url, save_path=annual_path)
        early_annual = {"url": u, "fiscal_year": fy, "info": info,
                        "path": annual_path}
        if early_sink is not None:
            early_sink["annual"] = early_annual

    def _passes_interim(info: dict) -> bool:
        # Relaxed gate for interim / half-year FINANCIAL reports: they are legitimately
        # shorter than an annual (the strict >=40pp floor would always reject them), but
        # must still be a recent, substantial document carrying a share-based-payment
        # term — so short earnings press releases are excluded.
        return (bool(info.get("ok"))
                and (info.get("fiscal_year") or 0) >= recent
                and (info.get("pages") or 0) >= 20
                and len(info.get("sbc_hits") or []) >= 1
                # same content check as the strict gate — a recent 57pp results
                # DECK satisfies every line above; the statement page doesn't lie.
                and bool(info.get("has_balance_sheet")))

    def _probe(u, label):
        nonlocal downloads
        if u in seen:
            return False
        seen.add(u)
        info = inspect_pdf(u, ir_url)          # probe only; save the winners afterward
        probed[u] = info
        downloads += 1
        ok = bool(info.get("ok") and info.get("stage1_would_accept"))
        # gate_note (gated), then reason (download/non-PDF failure — without
        # this those probes all print an opaque "FYNone"), then the FY tag.
        tag = (info.get("gate_note") or info.get("reason")
               or f"FY{info.get('fiscal_year')}")
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
        _secure_annual()
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
            probed_ok = _probe(u, "(search)")
            _secure_annual()
            if probed_ok and passers[-1][0] >= recent:
                break

    annuals = sorted([p for p in passers if p[1] == 1], reverse=True)
    interims = sorted([p for p in passers if p[1] == 0], reverse=True)

    out: dict = {"annual": None, "interim": None, "results": None,
                 "ir_url": ir_url}
    if early_annual is not None:
        # Already saved at pass time — reuse; never rewrite (see _secure_annual).
        out["annual"] = early_annual
    elif annuals and annual_path:
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


def fetch_all_reports(ir_url: str, out_dir: str, allow_fc: bool = True,
                      min_fy: int | None = None, max_downloads: int = 25,
                      name: str = "") -> dict:
    """Collect-mode variant of fetch_reports: ONE crawl, then probe EVERY
    annual/interim candidate and save ALL gate-passers into `out_dir` —
    annuals and quarterlies together, for later one-by-one use.

    The single-report freshness floor (MIN_FISCAL_YEAR) is replaced by
    `min_fy` (default CURRENT_YEAR - 3) so the archive can include prior
    years; the content gates are otherwise unchanged (annual: >=40pp with
    >=2 SBC terms; interim: >=20pp with >=1 SBC term). An undatable doc is
    not blocked on the year rule, mirroring inspect_pdf.

    Returns {"ir_url", "folder", "saved": [{url, fiscal_year, kind, pages,
    path}...], "probed": <n>} — `saved` newest-first.
    """
    import os
    min_fy = min_fy or (CURRENT_YEAR - 3)
    os.makedirs(out_dir, exist_ok=True)
    ranked_all = find_report_pdfs(ir_url, allow_fc)

    # Candidates = annual-scored (score>0) plus interim-NAMED (any score,
    # same rationale as fetch_reports), deduped by URL, newest-first.
    cands, seen_urls = [], set()
    for sc, u, a in ranked_all:
        named_interim = bool(re.search(_INTERIM_NAME_RE,
                                       (str(a) + " " + str(u)).lower()))
        if (sc > 0 or named_interim) and u not in seen_urls:
            seen_urls.add(u)
            cands.append((sc, u, a))
    cands.sort(key=lambda r: (_pdf_year(r[1], r[2]), r[0]), reverse=True)

    def _gate(info: dict) -> str | None:
        """Archive gate: kind ('annual'|'interim') or None. min_fy replaces
        the module freshness floor; a KNOWN year below it is rejected."""
        if not info.get("ok"):
            return None
        fy = info.get("fiscal_year") or 0
        if fy and fy < min_fy:
            return None
        pages = info.get("pages") or 0
        sbc = len(info.get("sbc_hits") or [])
        if pages >= 40 and sbc >= 2 and not (info.get("is_interim")
                                             and not info.get("is_annual")):
            return "annual"
        if info.get("is_interim") and pages >= 20 and sbc >= 1:
            return "interim"
        return None

    saved, seen_docs, downloads = [], set(), 0
    for i, (sc, u, a) in enumerate(cands):
        if downloads >= max_downloads:
            print(f"    collect: download cap ({max_downloads}) reached — "
                  f"{len(cands) - i} candidate(s) not probed")
            break
        # Save on the FIRST probe (force_save) to avoid re-downloading the
        # winners; rejects are deleted right after the gate.
        probe_path = os.path.join(out_dir, f"_probe_{i:02d}.pdf")
        info = inspect_pdf(u, ir_url, save_path=probe_path, force_save=True)
        downloads += 1
        kind = _gate(info)
        fy = info.get("fiscal_year") or _year_in(unquote(urlparse(u).path))
        if not kind:
            if fy and fy < min_fy:
                note = f"too old (FY{fy} < {min_fy})"
            else:
                note = (info.get("gate_note") or info.get("reason")
                        or "insufficient content")
            print(f"    collect reject: {note}  {u[:80]}")
            if os.path.exists(probe_path):
                os.remove(probe_path)
            continue
        # Same document linked under two URLs (mirrors) -> keep one copy.
        doc_key = (info.get("bytes"), info.get("pages"), fy, kind)
        if doc_key in seen_docs:
            print(f"    collect dup: FY{fy} {kind} already saved  {u[:80]}")
            os.remove(probe_path)
            continue
        seen_docs.add(doc_key)
        base = re.sub(r"[^A-Za-z0-9._-]+", "_",
                      os.path.basename(unquote(urlparse(u).path)))[:60] or f"{i:02d}.pdf"
        final = os.path.join(out_dir, f"FY{fy or 'unknown'}_{kind}_{base}")
        if not final.lower().endswith(".pdf"):
            final += ".pdf"
        n = 1
        while os.path.exists(final):
            final = re.sub(r"(\.pdf)$", f"_{n}.pdf", final, flags=re.I)
            n += 1
        os.replace(probe_path, final)
        print(f"    collect OK: FY{fy} {kind} {info.get('pages')}pp -> {final}")
        saved.append({"url": u, "fiscal_year": fy, "kind": kind,
                      "pages": info.get("pages"), "path": final})
    saved.sort(key=lambda s: (s["fiscal_year"] or 0), reverse=True)
    return {"ir_url": ir_url, "folder": out_dir, "saved": saved,
            "probed": downloads}


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
