"""
simply_route.py — standalone "Simply Wall St forecast" feature.

Completely separate from the options-extraction pipeline. Exposes an APIRouter
that backend.py mounts on the SAME origin (no new URL / tunnel):

  GET  /simply              -> a small self-contained HTML page (ticker form)
  GET  /api/simply          -> ticker [+ exchange] -> forecast rows as JSON
  GET  /api/simply/excel    -> the same forecast as a downloadable .xlsx

It reuses the scraper in Simply_wlst/data.py WITHOUT modifying it — data.py's
functions (session/find_url/get_state/extract_forecast) are imported by file
path so this stays isolated from the rest of the backend.
"""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

# ── Load Simply_wlst/data.py by path (no package import, stays isolated) ──
_DATA_PATH = Path(__file__).parent / "Simply_wlst" / "data.py"
_spec = importlib.util.spec_from_file_location("sws_data", _DATA_PATH)
_sws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sws)

# Forecast columns produced by data.py.extract_forecast.
# revenue / earnings / cfo are US$m; eps is per-share; date is YYYY-MM.
_COLUMNS = ["date", "revenue", "eps", "earnings", "cfo"]
_HEADERS = ["Period", "Revenue", "EPS", "Earnings", "CFO"]

router = APIRouter()


def _scrape(ticker: str, exchange: str | None):
    """Run the SWS scraper for one ticker. Returns (page_url, rows)."""
    ticker = (ticker or "").strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="A ticker symbol is required.")
    s = _sws.session()
    try:
        url = _sws.find_url(s, ticker, (exchange or "").strip() or None)
    except SystemExit as e:
        # data.py raises SystemExit when no page is found — surface as 404.
        raise HTTPException(status_code=404, detail=str(e))
    try:
        rows = _sws.extract_forecast(_sws.get_state(s, url))
    except SystemExit as e:
        # No December rows / page layout issue. Use 404 (not 5xx): Cloudflare
        # replaces 5xx bodies with its own HTML error page, which breaks the
        # frontend's JSON parsing. 404 is passed through with our message.
        raise HTTPException(status_code=404, detail=str(e))
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No analyst forecast rows found for {ticker.upper()}.",
        )
    return url, rows


@router.get("/api/simply")
def api_simply(
    ticker: str = Query(..., description="Ticker symbol, e.g. NVDA"),
    exchange: str | None = Query(None, description="Optional exchange hint: nasdaq, lse, xtra ..."),
):
    """Ticker [+ exchange] -> Simply Wall St forward forecast (JSON)."""
    url, rows = _scrape(ticker, exchange)
    return {"ticker": ticker.upper(), "page": url, "columns": _COLUMNS, "rows": rows}


@router.get("/api/simply/grouped")
def api_simply_grouped(
    ticker: str = Query(..., description="Ticker symbol, e.g. XOM"),
    exchange: str | None = Query(None, description="Optional exchange hint: nasdaq, lse, xtra ..."),
):
    """Same forecast, grouped per-metric as {period, value} arrays (rev_est, eps_est)."""
    _, rows = _scrape(ticker, exchange)

    def _series(col):
        # One {period, value} entry per row that carries this metric, in date order.
        return [{"period": r["date"], "value": r[col]} for r in rows if col in r]

    return {
        "ticker": ticker.upper(),
        "source": "Simply Wall St",
        "rev_est": _series("revenue"),
        "eps_est": _series("eps"),
    }


@router.get("/api/simply/excel")
def api_simply_excel(
    ticker: str = Query(...),
    exchange: str | None = Query(None),
):
    """The same forecast as a downloadable .xlsx (US$m)."""
    from openpyxl import Workbook

    _, rows = _scrape(ticker, exchange)
    wb = Workbook()
    ws = wb.active
    ws.title = "SWS Forecast"
    ws.append(_HEADERS)
    for r in rows:
        ws.append([r.get(c, "") for c in _COLUMNS])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"{ticker.strip().lower()}_sws_forecast.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Simply Wall St Forecast</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         background: #f4f6f8; color: #1c2530; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 40px 20px 80px; }
  h1 { font-size: 1.5rem; margin: 0 0 4px; }
  .sub { color: #5b6b7b; margin: 0 0 28px; font-size: .92rem; }
  form { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end;
         background: #fff; border: 1px solid #e1e6ec; border-radius: 12px; padding: 18px; }
  .field { display: flex; flex-direction: column; gap: 5px; }
  .field label { font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; color: #6b7a8a; }
  .field.grow { flex: 1 1 200px; }
  input { padding: 10px 12px; border: 1px solid #cdd5de; border-radius: 8px; font-size: 1rem; }
  input:focus { outline: 2px solid #2563eb; border-color: #2563eb; }
  button { padding: 10px 20px; border: 0; border-radius: 8px; background: #2563eb; color: #fff;
           font-size: 1rem; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: .55; cursor: default; }
  .msg { margin-top: 20px; font-size: .92rem; }
  .msg.err { color: #b42318; }
  .page-link { margin-top: 18px; font-size: .85rem; }
  .page-link a { color: #2563eb; }
  table { width: 100%; border-collapse: collapse; margin-top: 22px; background: #fff;
          border: 1px solid #e1e6ec; border-radius: 12px; overflow: hidden; }
  th, td { padding: 10px 14px; text-align: right; border-bottom: 1px solid #eef1f5; font-variant-numeric: tabular-nums; }
  th:first-child, td:first-child { text-align: left; }
  thead th { background: #f0f4f8; font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; color: #6b7a8a; }
  tbody tr:last-child td { border-bottom: 0; }
  .actions { margin-top: 18px; display: none; }
  .actions a { display: inline-block; padding: 9px 16px; border-radius: 8px; background: #0f766e;
               color: #fff; text-decoration: none; font-weight: 600; font-size: .9rem; }
  .foot { margin-top: 36px; font-size: .76rem; color: #8a98a6; line-height: 1.5; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Pavaki Forward Analyst Forecast</h1>
  <p class="sub">Enter a ticker symbol to retrieve forward-looking analyst consensus estimates &mdash; revenue, EPS, earnings, and operating cash flow.</p>

  <form id="f">
    <div class="field grow">
      <label for="ticker">Ticker symbol</label>
      <input id="ticker" name="ticker" placeholder="e.g. NVDA" autocomplete="off" required>
    </div>
    <button id="go" type="submit">Get forecast</button>
  </form>

  <div id="msg" class="msg"></div>
  <div id="pagelink" class="page-link"></div>
  <div id="result"></div>
  <div id="actions" class="actions"></div>

  <p class="foot">Source: Simply Wall St (S&amp;P data redistributed by SWS) &mdash; for personal model use.
     This tool is separate from the options-extraction app.</p>
</div>

<script>
const HEADERS = %HEADERS%;
const COLS = %COLS%;
const f = document.getElementById('f');
const msg = document.getElementById('msg');
const result = document.getElementById('result');
const actions = document.getElementById('actions');
const pagelink = document.getElementById('pagelink');
const go = document.getElementById('go');

f.addEventListener('submit', async (e) => {
  e.preventDefault();
  const ticker = document.getElementById('ticker').value.trim();
  if (!ticker) return;
  msg.className = 'msg'; msg.textContent = 'Fetching forecast…';
  result.innerHTML = ''; actions.style.display = 'none'; actions.innerHTML = ''; pagelink.innerHTML = '';
  go.disabled = true;
  const qs = new URLSearchParams({ ticker });
  try {
    const res = await fetch('/api/simply?' + qs.toString());
    const raw = await res.text();
    let data;
    try { data = JSON.parse(raw); }
    catch (_) {
      // Non-JSON (e.g. a gateway error page) — don't surface a raw parse error.
      throw new Error(res.ok ? 'Unexpected response from server.'
                             : ('Could not fetch forecast (' + res.status + '). Please try again.'));
    }
    if (!res.ok) throw new Error(data.detail || ('Request failed (' + res.status + ')'));
    msg.textContent = '';
    let html = '<table><thead><tr>' + HEADERS.map(h => '<th>' + h + '</th>').join('') + '</tr></thead><tbody>';
    for (const row of data.rows) {
      html += '<tr>' + COLS.map(c => '<td>' + (row[c] ?? '—') + '</td>').join('') + '</tr>';
    }
    html += '</tbody></table>';
    result.innerHTML = html;
    if (data.page) pagelink.innerHTML = 'SWS page: <a href="' + data.page + '" target="_blank" rel="noopener">' + data.page + '</a>';
    actions.innerHTML = '<a href="/api/simply/excel?' + qs.toString() + '">Download Excel</a>';
    actions.style.display = 'block';
  } catch (err) {
    msg.className = 'msg err';
    msg.textContent = err.message;
  } finally {
    go.disabled = false;
  }
});
</script>
</body>
</html>
"""


@router.get("/simply", response_class=HTMLResponse)
def simply_page():
    """Self-contained ticker form for the Simply Wall St forecast."""
    import json

    html = _PAGE.replace("%HEADERS%", json.dumps(_HEADERS)).replace("%COLS%", json.dumps(_COLUMNS))
    return HTMLResponse(content=html)
