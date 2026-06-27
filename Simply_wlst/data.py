#!/usr/bin/env python3
"""
sws_scraper.py — ticker -> Simply Wall St forward analyst consensus (revenue / EPS / earnings / CFO).

    python3 sws_scraper.py NVDA
    python3 sws_scraper.py MBG --exchange xtra
    python3 sws_scraper.py AUSS --exchange ob

How it works (fast, no browser, no API):
  1. find the company's SWS URL via a web search
  2. fetch the /future page HTML
  3. read the embedded window.__REACT_QUERY_STATE__ JSON
  4. pull the forward consensus from analysis.future.merged_future_* (December year-ends)
     plus the most recent December figure from the statement blob

These two SWS chart blocks are the source:
  - "Earnings and Revenue Growth Forecasts"      -> merged_future_revenue
  - "Earnings per Share Growth Forecasts"        -> merged_future_earnings_per_share

Note: numbers are S&P data redistributed by SWS — for personal model use, not redistribution.
"""

import argparse, csv, json, re, sys, time, random
from datetime import datetime, timezone
from urllib.parse import unquote

try:
    from curl_cffi import requests as http      # gets past Cloudflare
    IMP = "chrome"
except ImportError:
    import requests as http
    IMP = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Forward consensus series (keyed by date-in-ms) -> our column name.
FUTURE_SERIES = {"merged_future_revenue": "revenue",
                 "merged_future_earnings_per_share": "eps",
                 "merged_future_net_income": "earnings",
                 "merged_future_cash_operations": "cfo"}

# Statement-blob line items (for the most recent reported December year).
HIST = {"TOTAL_REV": "revenue", "BASIC_EPS": "eps", "NI": "earnings", "CASH_OPER": "cfo"}


def session():
    s = http.Session(impersonate=IMP) if IMP else http.Session()
    s.headers.update({"User-Agent": UA})
    return s


CACHE_FILE = "sws_url_cache.json"
SWS_PAT = re.compile(r"https://simplywall\.st/stocks/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+", re.I)
ENGINES = [("duckduckgo", "https://html.duckduckgo.com/html/"),
           ("bing", "https://www.bing.com/search")]


def _cache():
    try: return json.load(open(CACHE_FILE))
    except Exception: return {}


def _query(s, engine, url, q):
    if engine == "duckduckgo":
        return s.post(url, data={"q": q}, timeout=20).text
    return s.get(url, params={"q": q}, timeout=20).text          # Bing = GET


def _parse(html, ticker):
    for m in SWS_PAT.finditer(unquote(html)):
        u = re.sub(r"/(future|valuation|past|health|dividend|management|ownership|information|news.*)$", "", m.group(0).rstrip("/"))
        if f"-{ticker.lower()}" in u.split("/")[-2]:
            return u + "/future"          # Future Growth tab carries the forecast blocks
    return None


def _blocked(html):
    low = html.lower()
    return ("simplywall.st/stocks" not in unquote(html)
            and (len(html) < 20000 or "anomaly" in low or "unusual traffic" in low))


def find_url(s, ticker, exchange, debug=False):
    key = f"{ticker.upper()}|{(exchange or '').lower()}"
    cache = _cache()
    if key in cache:
        return cache[key]
    q = f"{ticker} {exchange or ''} Future Growth simply wall street".strip()
    for engine, eurl in ENGINES:
        for attempt in range(3):
            try:
                html = _query(s, engine, eurl, q)
            except Exception as e:
                if debug: print(f"[debug] {engine} error: {e}", file=sys.stderr)
                html = ""
            url = _parse(html, ticker)
            if url:
                cache[key] = url; json.dump(cache, open(CACHE_FILE, "w"))
                return url
            if _blocked(html):
                wait = 2 ** attempt + random.random()
                if debug: print(f"[debug] {engine} blocked ({len(html)}b), backoff {wait:.1f}s", file=sys.stderr)
                time.sleep(wait); continue
            break
    raise SystemExit(f"No SWS page found for {ticker}. Try --exchange, or run with --debug.")


def get_state(s, url):
    html = s.get(url, timeout=30).text
    return parse_state(html)


def parse_state(html):
    m = re.search(r"window\.__REACT_QUERY_STATE__\s*=\s*", html)
    if not m:
        raise SystemExit("Data blob not found on page (layout changed?).")
    blob = html[m.end():html.find("</script>", m.end())].strip().rstrip(";")
    blob = re.sub(r"(?<=[:,\[])undefined(?=[,}\]])", "null", blob)
    return json.loads(blob)


def _find_future(state):
    """Locate the analysis.future block (the one holding merged_future_* dicts)."""
    found = []
    def walk(n):
        if isinstance(n, dict):
            if isinstance(n.get("merged_future_revenue"), dict):
                found.append(n)
            for v in n.values(): walk(v)
        elif isinstance(n, list):
            for v in n: walk(v)
    walk(state)
    return found[0] if found else None


def _fye_month(fut):
    """Detect the fiscal year-end month = the most common month across forecast dates.
    The one off-cycle date (e.g. Mar/Apr) is the near-term interim stub, not an annual."""
    from collections import Counter
    months = Counter()
    for ms in (fut.get("merged_future_revenue") or {}):
        months[datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).month] += 1
    return months.most_common(1)[0][0] if months else 12


def _latest_fye_actual(state, fye_month):
    """Most recent fiscal year-end from the statement blob, matching the FYE month."""
    rows = {}
    def walk(n):
        if isinstance(n, dict):
            if n.get("quarter") == 4 and n.get("id") in HIST and "value" in n:
                ed = n["end_date"]; ed = ed / 1000 if ed > 1e12 else ed
                d = datetime.fromtimestamp(ed, tz=timezone.utc)
                if d.month == fye_month:
                    r = rows.setdefault(d.strftime("%Y-%m"), {"date": d.strftime("%Y-%m")})
                    col = HIST[n["id"]]
                    r[col] = round(n["value"], 2) if col == "eps" else round(n["value"])
            for v in n.values(): walk(v)
        elif isinstance(n, list):
            for v in n: walk(v)
    walk(state)
    return rows[max(rows)] if rows else None


def extract_forecast(state, with_last_actual=True):
    """Annual table (any fiscal year-end): forward consensus + latest reported year.
    FYE month is auto-detected per company, so Dec, Jan, Jun, etc. all work."""
    rows = {}
    fut = _find_future(state)
    fye = _fye_month(fut) if fut else 12
    if fut:
        for key, col in FUTURE_SERIES.items():
            for ms, val in (fut.get(key) or {}).items():
                d = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
                if d.month != fye:              # keep fiscal year-ends only, drop interim stub
                    continue
                r = rows.setdefault(d.strftime("%Y-%m"), {"date": d.strftime("%Y-%m")})
                r[col] = round(val, 2) if col == "eps" else round(val)
    if with_last_actual:
        a = _latest_fye_actual(state, fye)
        if a and a["date"] not in rows:
            rows[a["date"]] = a
    if not rows:
        raise SystemExit("No annual forecast/actual rows found (layout changed?).")
    return [rows[k] for k in sorted(rows)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--exchange", help="nasdaq, nyse, lse, xtra, ob, asx ...")
    ap.add_argument("--out", help="output file stem (default: <ticker>_est)")
    ap.add_argument("--no-actual", action="store_true", help="forecast years only, skip last reported year")
    ap.add_argument("--debug", action="store_true", help="show what the search returned")
    args = ap.parse_args()
    stem = args.out or f"{args.ticker.lower()}_est"

    s = session()
    url = find_url(s, args.ticker, args.exchange, args.debug)
    print("page:", url)
    rows = extract_forecast(get_state(s, url), with_last_actual=not args.no_actual)

    json.dump(rows, open(f"{stem}.json", "w"), indent=2)
    with open(f"{stem}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "revenue", "eps", "earnings", "cfo"],
                           extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

    print(f"\n{'FY':<9}{'Rev Est':>11}{'EPS Est':>9}{'Earnings':>10}{'CFO':>9}")
    for r in rows:
        print(f"{r['date']:<9}{r.get('revenue','-'):>11}{r.get('eps','-'):>9}"
              f"{r.get('earnings','-'):>10}{r.get('cfo','-'):>9}")
    print(f"\nwrote {stem}.json and {stem}.csv")


if __name__ == "__main__":
    main()