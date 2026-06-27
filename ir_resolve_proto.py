"""
ir_resolve_proto.py  —  STANDALONE prototype of the layered IR-homepage resolver.

Tests the robustness ideas WITHOUT touching tools.py / ca_ir_fetch.py / backend.py.

Layered stack (cheapest + most authoritative first; short-circuit on high confidence):
  Tier 1  Wikidata SPARQL : ticker / ISIN -> official website (P856)     [free, no key]
  Tier 2  Clearbit autocomplete : name -> domain                          [free, no key]
  Tier 3  ddgs search + rapidfuzz fuzzy scoring (the old approach, hardened)
  + consensus / confidence aggregation and an explicit ABSTAIN band.

(Tier 0 = Finnhub/FMP weburl is the real primary in production but needs a key;
 stubbed here so the architecture is visible.)
"""
from __future__ import annotations
import re, sys, json, time
import requests
from rapidfuzz import fuzz

UA = "options-extractor-ir-resolver/0.1 (research prototype)"
TIMEOUT = 15

LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "plc", "llc", "lp", "group", "holdings", "holding", "sa", "ag", "nv", "se",
    "spa", "ab", "asa", "oyj", "as", "kk", "bhd", "tbk", "pjsc", "psc", "the",
}
# multi-label public suffixes we care about (no tldextract installed)
MULTI_TLD = {
    "co.uk", "com.cn", "co.jp", "com.hk", "com.br", "co.kr", "com.tw", "com.au",
    "co.in", "com.sg", "com.my", "co.za", "com.tr", "co.il", "com.sa", "co.id",
    "com.mx", "co.nz", "com.ph", "com.ar",
}
BLOCKED = {
    "sec.gov", "yahoo.com", "finance.yahoo.com", "bloomberg.com", "reuters.com",
    "marketwatch.com", "seekingalpha.com", "nasdaq.com", "nyse.com", "wsj.com",
    "investing.com", "morningstar.com", "globenewswire.com", "prnewswire.com",
    "businesswire.com", "wikipedia.org", "linkedin.com", "facebook.com",
    "twitter.com", "x.com", "annualreports.com", "stockanalysis.com",
    "tradingview.com", "barrons.com", "fool.com", "simplywall.st", "macrotrends.net",
}
IR_PATH_RE = re.compile(r"/(investor|investors|investor-relations|ir|shareholder)", re.I)

# ccTLD -> country (for the P0-fix #3 country sanity check). gTLDs (.com/.net/.org) are neutral.
CCTLD_COUNTRY = {
    "ca": "Canada", "uk": "United Kingdom", "au": "Australia", "kr": "South Korea",
    "jp": "Japan", "cn": "China", "hk": "Hong Kong", "tw": "Taiwan", "in": "India",
    "id": "Indonesia", "br": "Brazil", "de": "Germany", "fr": "France", "nl": "Netherlands",
    "se": "Sweden", "il": "Israel", "sa": "Saudi Arabia", "sg": "Singapore", "my": "Malaysia",
    "th": "Thailand", "za": "South Africa", "mx": "Mexico", "ch": "Switzerland",
}

# Curated SGX counter-code -> company name. SGX has no clean public ticker API (the
# portal is bot-walled), so a small VERIFIED map lets a bare code (e.g. D05) resolve
# to a real company name before the generic resolver runs — codes are meaningless to
# web search on their own. Same precedent as the EU/Israel/Malaysia maps. Holds only
# well-established issuers (mostly STI constituents); a WRONG entry would silently
# fetch the wrong company, so unverified codes are deliberately excluded. A code not
# in the map falls through to the normal resolver (with the Singapore search bias).
SGX_TICKER_MAP = {
    "D05": "DBS Group Holdings", "O39": "Oversea-Chinese Banking Corporation OCBC",
    "U11": "United Overseas Bank UOB", "Z74": "Singtel Singapore Telecommunications",
    "C6L": "Singapore Airlines", "S68": "Singapore Exchange SGX",
    "F34": "Wilmar International", "BN4": "Keppel", "S63": "Singapore Technologies Engineering",
    "9CI": "CapitaLand Investment", "U96": "Sembcorp Industries", "G13": "Genting Singapore",
    "C07": "Jardine Cycle and Carriage", "BS6": "Yangzijiang Shipbuilding", "S58": "SATS",
    "V03": "Venture Corporation", "U14": "UOL Group", "Y92": "Thai Beverage",
    "C38U": "CapitaLand Integrated Commercial Trust", "A17U": "CapitaLand Ascendas REIT",
    "M44U": "Mapletree Logistics Trust", "ME8U": "Mapletree Industrial Trust",
    "N2IU": "Mapletree Pan Asia Commercial Trust", "H78": "Hongkong Land",
    "J36": "Jardine Matheson Holdings", "D01": "DFI Retail Group", "TQ5": "Frasers Property",
    "5E2": "Seatrium", "EB5": "First Resources", "AJBU": "Keppel DC REIT",
}

# Curated BMV (Bolsa Mexicana de Valores) ticker -> company name. Same rationale and
# rules as SGX_TICKER_MAP: well-established issuers only, verified (a wrong entry would
# silently fetch the wrong company). Series suffixes (B/CPO/UBD/A1/*) are dropped to the
# base code by the resolver before lookup.
MEX_TICKER_MAP = {
    "AMX": "America Movil", "WALMEX": "Walmart de Mexico Walmex",
    "FEMSA": "Fomento Economico Mexicano FEMSA", "KOF": "Coca-Cola FEMSA",
    "GFNORTE": "Grupo Financiero Banorte", "GMEXICO": "Grupo Mexico", "CEMEX": "CEMEX",
    "BIMBO": "Grupo Bimbo", "TLEVISA": "Grupo Televisa", "ORBIA": "Orbia Advance",
    "AC": "Arca Continental", "GRUMA": "Gruma", "KIMBER": "Kimberly-Clark de Mexico",
    "ALSEA": "Alsea", "LIVEPOL": "El Puerto de Liverpool", "ELEKTRA": "Grupo Elektra",
    "GCARSO": "Grupo Carso", "PINFRA": "Promotora y Operadora de Infraestructura",
    "GAP": "Grupo Aeroportuario del Pacifico", "ASUR": "Grupo Aeroportuario del Sureste",
    "OMA": "Grupo Aeroportuario Centro Norte OMA", "CUERVO": "Becle Jose Cuervo",
    "MEGA": "Megacable", "Q": "Qualitas Controladora", "BBAJIO": "Banco del Bajio",
    "GENTERA": "Gentera", "LACOMER": "La Comer", "VESTA": "Corporacion Inmobiliaria Vesta",
}

# Markets that reuse the Diamond IR-scraper but are LOCKED to one country (no dedicated
# data API). For these: an exchange-prefixed ticker is normalized to a bare code, the
# curated map can anchor a bare code to a company name, the search is biased to the
# exchange + country, and the resolver is STRICT (verify or abstain) so an unknown code
# never blind-fetches a wrong-entity guess. Keyed by lowercased country.
_STRICT_LOCALES = {
    "singapore": {"exch": "SGX", "map": SGX_TICKER_MAP, "extra": "annual report"},
    "mexico":    {"exch": "BMV", "map": MEX_TICKER_MAP, "extra": "annual report informe anual"},
}


def tokens(name: str, ticker: str) -> set[str]:
    toks = {t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) >= 3 and t not in LEGAL_SUFFIXES}
    if ticker:
        toks.add(ticker.lower())
    return toks


def registrable(host: str) -> str:
    host = host.lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last2 = ".".join(parts[-2:])
    if last2 in MULTI_TLD:
        return ".".join(parts[-3:])
    return last2


def host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1).lower() if m else ""


def is_blocked(url: str) -> bool:
    h = host_of(url)
    reg = registrable(h)
    return reg in BLOCKED or h in BLOCKED


# ---------------------------------------------------------------- Tier 1: Wikidata
def _wikidata_sparql(ticker, isin):
    clauses = []
    if isin:
        clauses.append(f'{{ ?c wdt:P946 "{isin}" . }}')
    if ticker:
        clauses.append(f'{{ ?c wdt:P249 "{ticker}" . }}')
    if not clauses:
        return None
    q = f"""SELECT ?c ?cLabel ?w WHERE {{
      {" UNION ".join(clauses)}
      ?c wdt:P856 ?w .
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }} LIMIT 5"""
    r = requests.get("https://query.wikidata.org/sparql",
                     params={"query": q, "format": "json"},
                     headers={"User-Agent": UA, "Accept": "application/sparql-results+json"},
                     timeout=TIMEOUT)
    if r.status_code != 200:
        return None
    for row in r.json().get("results", {}).get("bindings", []):
        url = row.get("w", {}).get("value")
        if url and not is_blocked(url):
            return {"url": url, "label": row.get("cLabel", {}).get("value", ""), "source": "wikidata"}
    return None


def _wikidata_by_name(name):
    """name -> top entity (wbsearchentities) -> official website P856 (wbgetentities)."""
    r = requests.get("https://www.wikidata.org/w/api.php",
                     params={"action": "wbsearchentities", "search": name, "language": "en",
                             "type": "item", "limit": 3, "format": "json"},
                     headers={"User-Agent": UA}, timeout=TIMEOUT)
    if r.status_code != 200:
        return None
    for hit in r.json().get("search", []):
        qid = hit.get("id")
        if not qid:
            continue
        # only accept if the label fuzzily matches (avoid grabbing a same-name unrelated item)
        if fuzz.token_set_ratio(name.lower(), hit.get("label", "").lower()) < 70:
            continue
        c = requests.get("https://www.wikidata.org/w/api.php",
                         params={"action": "wbgetentities", "ids": qid, "props": "claims", "format": "json"},
                         headers={"User-Agent": UA}, timeout=TIMEOUT)
        if c.status_code != 200:
            continue
        claims = c.json().get("entities", {}).get(qid, {}).get("claims", {})
        for snak in claims.get("P856", []):
            url = snak.get("mainsnak", {}).get("datavalue", {}).get("value")
            if url and not is_blocked(url):
                return {"url": url, "label": hit.get("label", ""), "source": "wikidata"}
    return None


def wikidata_lookup(name: str, ticker: str | None, isin: str | None) -> dict | None:
    """ticker/ISIN -> P856; falls back to name->entity->P856. Returns {url,...} or None."""
    try:
        hit = _wikidata_sparql(ticker, isin)
        if hit:
            return hit
        return _wikidata_by_name(name)
    except Exception as e:
        print(f"    [wikidata err: {e}]", file=sys.stderr)
    return None


# ---------------------------------------------------------------- Tier 2: Clearbit
def clearbit_lookup(name: str) -> dict | None:
    """name -> domain via Clearbit autocomplete (free, no key)."""
    try:
        r = requests.get(
            "https://autocomplete.clearbit.com/v1/companies/suggest",
            params={"query": name}, headers={"User-Agent": UA}, timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None
        for item in r.json():
            dom = item.get("domain")
            if dom and registrable(dom) not in BLOCKED:
                score = fuzz.token_set_ratio(name.lower(), item.get("name", "").lower())
                if score < 75:        # P0-fix #4: junk floor — drop weak name->domain guesses
                    continue
                return {"url": f"https://{dom}", "label": item.get("name", ""),
                        "fuzzy": score, "source": "clearbit"}
    except Exception as e:
        print(f"    [clearbit err: {e}]", file=sys.stderr)
    return None


# ---------------------------------------------------------------- Tier 3: search + fuzzy
def ddg_search(queries: list[str], max_results: int = 8) -> list[str]:
    urls = []
    try:
        from ddgs import DDGS
        with DDGS() as d:
            for q in queries:
                try:
                    for hit in d.text(q, max_results=max_results):
                        u = hit.get("href") or hit.get("url") or hit.get("link")
                        if u:
                            urls.append(u)
                except Exception as e:
                    print(f"    [ddg query err: {e}]", file=sys.stderr)
                time.sleep(1.0)  # respect DDG pacing
    except Exception as e:
        print(f"    [ddg init err: {e}]", file=sys.stderr)
    return urls


def score_candidate(url: str, name: str, ticker: str, toks: set[str]) -> float:
    if is_blocked(url):
        return -100
    host = host_of(url)
    reg = registrable(host)
    reg_label = reg.split(".")[0]
    path = url[len(host) + 8:] if "://" in url else url
    s = 0.0
    # fuzzy: name vs registrable label (graded, not binary)
    name_clean = " ".join(sorted(toks - ({ticker.lower()} if ticker else set())))
    s += fuzz.token_set_ratio(name_clean, reg_label) * 0.12        # up to ~12
    s += fuzz.partial_ratio(name_clean.replace(" ", ""), reg_label) * 0.06  # up to ~6
    if ticker and ticker.lower() == reg_label:
        s += 10
    if ticker and f"invest-{ticker.lower()}" in host:
        s += 8
    if any(t in reg_label for t in toks):
        s += 4
    if host.startswith(("ir.", "investor.", "investors.")):
        s += 6
    if IR_PATH_RE.search(path):
        s += 5
    if url.startswith("https://"):
        s += 1
    if re.search(r"(archive|legacy|old)\.", host):
        s -= 8
    if re.search(r"/(contact|privacy|terms|careers)", path, re.I):
        s -= 6
    return s


def search_resolve(name: str, ticker: str, country: str | None) -> dict | None:
    toks = tokens(name, ticker)
    cc = f" {country}" if country else ""
    queries = [
        f"{name} {ticker}{cc} official investor relations site",
        f"{name} investor relations{cc}",
    ]
    # Country-locked-market bias: exchange codes are cryptic, so anchor the search on the
    # exchange + country so it locks onto the real issuer's own site (not a same-token firm).
    _loc = _STRICT_LOCALES.get((country or "").strip().lower())
    if _loc:
        queries.insert(0, f"{name} {_loc['exch']}:{ticker} {country} {_loc['extra']} investor relations")
    cand = {}
    for u in ddg_search(queries):
        host = host_of(u)
        if not host:
            continue
        canon = f"https://{host}{('/' + u.split(host,1)[1].split('/',1)[1].split('?')[0]) if host in u and '/' in u.split(host,1)[1] else ''}"
        sc = score_candidate(u, name, ticker, toks)
        if canon not in cand or sc > cand[canon]:
            cand[canon] = sc
    if not cand:
        return None
    best = max(cand, key=cand.get)
    ranked = sorted(cand.values(), reverse=True)
    runner = ranked[1] if len(ranked) > 1 else 0
    return {"url": best, "score": cand[best], "runner_up": runner, "source": "search"}


# ---------------------------------------------------------------- verification (P0-fix #2)
def verify_homepage(url: str, name: str, ticker: str, toks: set[str]) -> bool | None:
    """Fetch the chosen page; True if name/ticker actually appears, False if clearly absent,
    None if the page couldn't be fetched (don't penalise transient network failures)."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code >= 400:
            return None
        text = r.text.lower()
        title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S)
        title_txt = (title.group(1) if title else "")[:300]
        if ticker and ticker.lower() in text:
            return True
        name_toks = toks - ({ticker.lower()} if ticker else set())
        if any(t in text for t in name_toks if len(t) >= 4):
            return True
        if fuzz.token_set_ratio(name.lower(), title_txt) >= 60:
            return True
        return False
    except Exception:
        return None


def cctld_country(reg: str) -> str | None:
    parts = reg.split(".")
    return CCTLD_COUNTRY.get(parts[-1])


# ---------------------------------------------------------------- LLM entity gate
# Reuse the project's Together AI classifier (same provider/model the Stage-2 page
# classifier uses). OpenAI-compatible endpoint; model overridable via TOGETHER_MODEL.
LLM_PICK_MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
TOGETHER_BASE_URL = "https://api.together.xyz/v1"


def _llm_pick_url(name: str, ticker: str, country: str,
                  candidates: list[tuple[str, str]]) -> dict | None:
    """Cheap LLM URL/entity classifier (Together AI / Llama-3.3-70B). Given the
    candidate websites, pick the ONE that is the official site of THIS EXACT company,
    or abstain (0) when a candidate only shares a keyword/ticker with a different
    company (e.g. 'INB'/'invest bank' -> State Bank of India). Text classification
    only — NOT report extraction.

    Returns {"url": <str|None>, "reason": <str>} or None if the LLM is unavailable
    (no key / error) so the caller falls back to the heuristic result."""
    if not candidates:
        return None
    try:
        import os
        from openai import OpenAI
    except Exception:
        return None
    key = os.environ.get("TOGETHER_API_KEY")
    if not key or key == "your_together_key_here":
        return None
    model = os.environ.get("TOGETHER_MODEL", LLM_PICK_MODEL)
    lines = "\n".join(f"{i + 1}. {u}  (found via {s})"
                      for i, (u, s) in enumerate(candidates))
    prompt = (
        f"Company: {name or '(unknown)'}\n"
        f"Ticker: {ticker or '(none)'}\n"
        f"Country: {country or '(unknown)'}\n\n"
        f"Candidate websites:\n{lines}\n\n"
        "Which ONE candidate is the OFFICIAL corporate or investor-relations website "
        "of THIS EXACT company? REJECT any candidate that belongs to a DIFFERENT "
        "company that merely shares a keyword or ticker code (e.g. a larger bank, an "
        "investment fund, a subsidiary, or an unrelated firm in another country). "
        "If none of the candidates clearly belong to this exact company, answer 0.\n"
        'Respond ONLY with JSON: {"choice": <integer 0..N>, "reason": "<brief>"}'
    )
    try:
        client = OpenAI(api_key=key, base_url=TOGETHER_BASE_URL)
        resp = client.chat.completions.create(
            model=model, max_tokens=200, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        txt = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", txt, re.S)
        data = json.loads(m.group(0)) if m else {}
        choice = int(data.get("choice", 0))
        reason = str(data.get("reason", ""))[:120]
        if 1 <= choice <= len(candidates):
            return {"url": candidates[choice - 1][0], "reason": reason}
        return {"url": None, "reason": reason or "no candidate matched"}
    except Exception as e:
        print(f"    [llm pick err: {e}]", file=sys.stderr)
        return None


def _llm_suggest_domain(name: str, ticker: str, country: str) -> list[str]:
    """Ask the LLM for the company's OWN official website domain(s). Used only as a
    recovery fallback when web-search didn't surface the right site. The LLM knows
    e.g. inb.ae = Invest Bank even when ddg returns only State Bank of India.
    Returns up to 2 bare domains (best first), or [] if unavailable/unsure. The
    caller MUST verify each before trusting it (guards against hallucination)."""
    try:
        import os
        from openai import OpenAI
    except Exception:
        return []
    key = os.environ.get("TOGETHER_API_KEY")
    if not key or key == "your_together_key_here":
        return []
    model = os.environ.get("TOGETHER_MODEL", LLM_PICK_MODEL)
    prompt = (
        f"Company: {name or '(unknown)'}\n"
        f"Ticker: {ticker or '(none)'}\n"
        f"Country: {country or '(unknown)'}\n\n"
        "Give the OFFICIAL corporate website domain(s) of THIS EXACT company "
        '(e.g. "example.com"), best guess first, up to 2. Use the company\'s own '
        "registered domain — NOT a stock-exchange, news, or aggregator site, and NOT "
        "a different company that merely shares a keyword/ticker. If you are not "
        "confident, return an empty list.\n"
        'Respond ONLY with JSON: {"domains": ["..."], "reason": "<brief>"}'
    )
    try:
        client = OpenAI(api_key=key, base_url=TOGETHER_BASE_URL)
        resp = client.chat.completions.create(
            model=model, max_tokens=200, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        txt = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", txt, re.S)
        data = json.loads(m.group(0)) if m else {}
        return [str(d) for d in (data.get("domains") or []) if d][:2]
    except Exception as e:
        print(f"    [llm domain err: {e}]", file=sys.stderr)
        return []


def _verify_suggested(url: str, name: str, ticker: str, toks: set[str]) -> bool | None:
    """Like verify_homepage but uses a real Chrome TLS fingerprint (curl_cffi) so it
    matches what the fetcher can actually reach (many IR sites reject plain requests).
    True = company appears on the page, False = page is a different company, None =
    couldn't fetch."""
    text = None
    try:
        from curl_cffi import requests as _creq
        r = _creq.get(url, impersonate="chrome", timeout=TIMEOUT, allow_redirects=True)
        if r.status_code < 400:
            text = r.text.lower()
    except Exception:
        text = None
    if text is None:
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            if r.status_code < 400:
                text = r.text.lower()
        except Exception:
            return None
    if text is None:
        return None
    # Robust match. Generic substrings (a 3-letter ticker, or words like
    # "first"/"bank") are NOT trustworthy on their own (fab.com = the brand "Fab",
    # not First Abu Dhabi Bank, but contains "first"+"bank"). Require either:
    #   (a) the page <title> fuzzy-matches the company name, or
    #   (b) the two most DISTINCTIVE (>=5-char) name tokens BOTH appear (e.g. for
    #       "First Abu Dhabi Bank" that's "first"+"dhabi" — "dhabi" is the discriminator).
    mt = re.search(r"<title[^>]*>(.*?)</title>", text, re.S)
    title = (mt.group(1) if mt else "")[:300]
    if title and fuzz.token_set_ratio(name.lower(), title) >= 75:
        return True
    distinctive = sorted({t for t in (toks - ({ticker.lower()} if ticker else set()))
                          if len(t) >= 5}, key=len, reverse=True)
    if len(distinctive) >= 2 and all(t in text for t in distinctive[:2]):
        return True
    return False


def _llm_domain_fallback(name: str, ticker: str, country: str) -> dict | None:
    """Recovery: ask the LLM for the official domain, then VERIFY before trusting.
    Accept a domain if the page confirms the company, OR (when the page can't be
    fetched) the domain label structurally matches the ticker/name (e.g. inb.ae ->
    label 'inb' == ticker INB). Reject if the page is clearly a different company.
    Returns {"url", "verified", ...} or None — None keeps the caller's safe abstain."""
    # Country-locked markets (Singapore/Mexico) are STRICT: only accept a suggested domain
    # whose page actually verifies the company. The lenient "accept on structural
    # name_match when the page can't be fetched" rule (kept for other markets to recover
    # fetch-blocked sites like inb.ae) is exactly what let the hallucinated, non-existent
    # 'omsetech.com' through for SGX:OMSE (token "omse" is a substring of "omsetech").
    # Forcing verification means a fabricated/dead domain is rejected and we abstain.
    strict = (country or "").strip().lower() in _STRICT_LOCALES
    toks = tokens(name, ticker)
    for dom in _llm_suggest_domain(name, ticker, country):
        dom = re.sub(r"^https?://", "", str(dom)).strip().strip("/").split("/")[0].lower()
        if not dom or "." not in dom or registrable(dom) in BLOCKED:
            continue
        reg = registrable(dom)
        label = reg.split(".")[0]
        name_match = bool((ticker and ticker.lower() == label)
                          or any(t in label for t in toks if len(t) >= 3))
        url = f"https://{dom}/"
        v = _verify_suggested(url, name, ticker, toks)
        if v is True or (not strict and v is not False and name_match):
            return {"url": url, "verified": bool(v is True), "name_match": name_match}
    return None


# ---------------------------------------------------------------- Orchestrator
def resolve(name: str, ticker: str = "", isin: str = "", country: str = "") -> dict:
    # Country-locked scraper markets (Singapore/SGX, Mexico/BMV, ...): the ticker arrives
    # exchange-prefixed (e.g. "SGX:Z77" / "BMV:WALMEX"). Use the bare code for matching,
    # and for a bare code with no usable company name look up the real name from the
    # curated map so the resolver has a strong anchor (a bare code is meaningless to
    # web search; the company name is not).
    loc = _STRICT_LOCALES.get((country or "").strip().lower())
    strict_unanchored = False
    if loc:
        anchored = len((name or "").strip()) >= 3
        if ticker:
            code = ticker.split(":")[-1].strip().upper()
            if code:
                ticker = code
                mapped = loc["map"].get(code)
                if mapped and not anchored:
                    name = mapped
                    anchored = True   # the curated map is a trustworthy anchor
        # A bare, UNKNOWN code with no company name isn't enough to safely identify the
        # issuer — stay strict and abstain below unless we reach HIGH (consensus).
        strict_unanchored = not anchored

    evidence = []
    domains = {}  # registrable -> [sources]

    def note(reg, src):
        domains.setdefault(reg, []).append(src)

    wd = wikidata_lookup(name, ticker or None, isin or None)
    if wd:
        evidence.append(wd); note(registrable(host_of(wd["url"])), "wikidata")
    cb = clearbit_lookup(name)
    if cb:
        evidence.append(cb); note(registrable(host_of(cb["url"])), "clearbit")
    sr = search_resolve(name, ticker, country)
    if sr:
        evidence.append(sr); note(registrable(host_of(sr["url"])), "search")

    if not evidence:
        fb = _llm_domain_fallback(name, ticker, country)
        if fb:
            return {"company": name, "ticker": ticker, "chosen_url": fb["url"],
                    "registrable": registrable(host_of(fb["url"])),
                    "backers": ["llm-domain"], "confidence": "MEDIUM (llm-domain)",
                    "flags": [f"llm-domain-suggested(verified={fb['verified']})"],
                    "evidence": []}
        return {"company": name, "confidence": "ABSTAIN", "reason": "no candidates", "evidence": []}

    def quality(ev):
        """tier (3 strong .. 0 weak), raw — used to rank evidence within/ across domains."""
        url, src = ev["url"], ev["source"]
        ir = bool(IR_PATH_RE.search(url)) or host_of(url).startswith(("ir.", "investor", "investors."))
        if src == "wikidata":
            return (3, 100)                                   # authoritative
        if src == "search" and ir:
            return (3, ev.get("score", 0))                    # an actual IR page on an own-domain
        if src == "clearbit" and ev.get("fuzzy", 0) >= 85:
            return (2, ev.get("fuzzy", 0))
        if src == "search" and ev.get("score", 0) >= 15:
            return (2, ev.get("score", 0))
        if src == "clearbit":
            return (1, ev.get("fuzzy", 0))                    # weak name->domain guess
        return (0, ev.get("score", 0))

    def _has_ir(e):
        return bool(IR_PATH_RE.search(e["url"])) or host_of(e["url"]).startswith(("ir.", "investor", "investors."))

    # rank each domain by: (#backers, hosts-an-IR-page, best evidence tier, best raw).
    # has-IR-page beats a bare authoritative homepage on a different domain
    # (e.g. Alibaba: alibabagroup.com/ir > alibaba.com marketplace root).
    def domain_rank(reg):
        evs = [e for e in evidence if registrable(host_of(e["url"])) == reg]
        best_q = max(quality(e) for e in evs)
        has_ir = any(_has_ir(e) for e in evs)
        return (len(set(domains[reg])), int(has_ir), best_q[0], best_q[1])

    best_reg = max(domains, key=domain_rank)
    backers = set(domains[best_reg])
    win_evs = [e for e in evidence if registrable(host_of(e["url"])) == best_reg]
    chosen = max(win_evs, key=quality)
    ctier = quality(chosen)[0]

    # P0-fix #1: return the IR sub-page on the winning domain, not the bare root.
    ir_urls = [e["url"] for e in win_evs
               if IR_PATH_RE.search(e["url"]) or host_of(e["url"]).startswith(("ir.", "investor", "investors."))]
    # prefer the shallowest IR path (the IR landing page, not a deep news article)
    ir_urls.sort(key=lambda u: (u.rstrip("/").count("/"), len(u)))
    display_url = ir_urls[0] if ir_urls else chosen["url"]

    # confidence band
    if len(backers) >= 2:
        conf = "HIGH (consensus)"
    elif "wikidata" in backers and ctier == 3:
        conf = "HIGH (authoritative)"
    elif ctier >= 2:
        conf = "MEDIUM"
    else:
        conf = "LOW (confirm manually)"

    flags = []
    # P0-fix #3: country sanity check via ccTLD (gTLDs are neutral, never flagged).
    dom_cc = cctld_country(best_reg)
    if country and dom_cc and dom_cc != country:
        flags.append(f"country-mismatch(domain={dom_cc} vs {country})")
        conf = "LOW (confirm manually)"
    # P0-fix #2: verify the page actually mentions the company.
    toks = tokens(name, ticker)
    v = verify_homepage(display_url, name, ticker, toks)
    if v is False:
        flags.append("verify-FAILED(name/ticker not on page)")
        conf = "LOW (confirm manually)"
    elif v is None:
        flags.append("verify-skipped(fetch failed)")

    # LLM ENTITY GATE (Option 1): the heuristic verification only checks generic
    # tokens (e.g. "invest"/"bank" appear on ANY bank's site, so "invest bank P.S.C."
    # wrongly verified against State Bank of India). Have a cheap LLM pick the candidate
    # that truly belongs to THIS exact company, or abstain — overriding the heuristic.
    cand_urls, seen_c = [], set()
    for e in evidence:
        if e["url"] not in seen_c:
            seen_c.add(e["url"]); cand_urls.append((e["url"], e["source"]))
    llm = _llm_pick_url(name, ticker, country, cand_urls)
    if llm is not None:
        if llm["url"]:
            chosen_reg = registrable(host_of(llm["url"]))
            reg_evs = [e for e in evidence
                       if registrable(host_of(e["url"])) == chosen_reg]
            ir_urls2 = [e["url"] for e in reg_evs
                        if IR_PATH_RE.search(e["url"])
                        or host_of(e["url"]).startswith(("ir.", "investor", "investors."))]
            ir_urls2.sort(key=lambda u: (u.rstrip("/").count("/"), len(u)))
            display_url = ir_urls2[0] if ir_urls2 else llm["url"]
            best_reg = chosen_reg
            flags.append(f"llm-selected: {llm['reason']}")
            if conf.startswith("LOW"):
                conf = "MEDIUM (llm-selected)"
        else:
            # LLM rejected every candidate as a different company. Before abstaining,
            # ask the LLM for the company's OWN domain (verified) — recovers the right
            # site when web-search only surfaced a wrong-entity (the INB->SBI case where
            # search returned State Bank of India but not inb.ae).
            fb = _llm_domain_fallback(name, ticker, country)
            if fb:
                display_url = fb["url"]
                best_reg = registrable(host_of(fb["url"]))
                flags.append(f"llm-domain-suggested(verified={fb['verified']})")
                conf = "MEDIUM (llm-domain)"
            else:
                flags.append(f"llm-rejected-all: {llm['reason']}")
                conf = "LOW (confirm manually)"

    result = {
        "company": name, "ticker": ticker, "chosen_url": display_url,
        "registrable": best_reg, "backers": sorted(backers), "confidence": conf,
        "flags": flags, "evidence": evidence,
    }
    # Abstain on LOW confidence: don't hand back a URL the caller will blindly fetch.
    # A LOW band means the only backer was a weak search/clearbit guess (tier < 2) or a
    # sanity check failed (country-mismatch / verify-FAILED) — fetching it risks pulling
    # data from the WRONG company. Keep the guess under low_conf_guess for diagnostics.
    if conf.startswith("LOW"):
        result["low_conf_guess"] = display_url
        result["chosen_url"] = None
    # Locale strictness: an unknown bare exchange code with no company name can still
    # yield a confident-looking search/LLM pick that's the WRONG entity (e.g. SGX:OMSE ->
    # Amos Group, unverified). Require HIGH (consensus/authoritative) or abstain and ask
    # for the company name — never blind-fetch an unanchored guess.
    if strict_unanchored and not conf.startswith("HIGH"):
        result.setdefault("low_conf_guess", display_url)
        result["chosen_url"] = None
        result["confidence"] = f"ABSTAIN (unknown {loc['exch']} code — enter the company name)"
        flags.append("locale-unanchored-abstain")
    return result


BASKET = [
    # name, ticker, isin, country, note
    ("Canadian Tire Corporation", "CTC.A", "", "Canada", "FAMILY trap (vs CT REIT)"),
    ("Block Inc", "XYZ", "", "United States", "common-noun name"),
    ("Shell plc", "SHEL", "", "United Kingdom", "common-noun name"),
    ("Moderna Inc", "MRNA", "", "United States", "clean case"),
    ("Dollarama Inc", "DOL", "", "Canada", "clean case (prototype worked)"),
    ("Samsung Electronics", "005930", "", "South Korea", "non-English issuer"),
]

if __name__ == "__main__":
    for name, tk, isin, cc, why in BASKET:
        print(f"\n{'='*70}\n{name}  [{tk}]  — {why}")
        out = resolve(name, tk, isin, cc)
        print(f"  -> {out.get('chosen_url','(none)')}")
        print(f"     domain={out.get('registrable')}  backers={out.get('backers')}  CONF={out['confidence']}")
        if out.get("flags"):
            print(f"     flags={out['flags']}")
        for ev in out.get("evidence", []):
            extra = f" fuzzy={ev['fuzzy']}" if "fuzzy" in ev else (f" score={ev['score']:.0f}/runner={ev['runner_up']:.0f}" if "score" in ev else "")
            print(f"       [{ev['source']:9}] {ev['url']}{extra}")
