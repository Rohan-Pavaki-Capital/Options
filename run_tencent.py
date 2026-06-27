"""One-off driver: resolve + download Tencent's latest annual report and SAVE the PDF."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import ir_resolve_proto as R
import ir_fetch_proto as F

NAME, TICKER, COUNTRY = "Tencent Holdings Limited", "HTCD", "Hong Kong"
SAVE_PATH = "_ir_HTCD_Tencent.pdf"

print("="*70, "\nRESOLVE\n", "="*70)
res = R.resolve(NAME, TICKER, "", COUNTRY)
print(f"  chosen_url : {res.get('chosen_url')}")
print(f"  domain     : {res.get('registrable')}  backers={res.get('backers')}")
print(f"  confidence : {res['confidence']}   flags={res.get('flags')}")
for ev in res.get("evidence", []):
    extra = (f" fuzzy={ev['fuzzy']:.0f}" if "fuzzy" in ev
             else f" score={ev['score']:.0f}/runner={ev['runner_up']:.0f}" if "score" in ev else "")
    print(f"     [{ev['source']:9}] {ev['url']}{extra}")

ir_url = res.get("chosen_url")
print("\n", "="*70, "\nFETCH (annual report)\n", "="*70)
ranked = F.find_report_pdfs(ir_url, allow_fc=True)
if not ranked:
    print("  no PDF candidates found")
    sys.exit(0)
print("  top candidates:")
for sc, u, a in ranked[:6]:
    print(f"    [{sc:+.0f}] {a[:40]!r}  {u[:95]}")

# walk candidates best-first; download+gate; save the first that Stage-1 accepts
for sc, u, a in ranked:
    if sc <= 0:
        print("  remaining candidates score <=0 -> stop")
        break
    print(f"\n  trying: [{sc:+.0f}] {u}")
    info = F.inspect_pdf(u, ir_url, save_path=SAVE_PATH)
    print(f"  inspect: {info}")
    if info.get("ok") and info.get("stage1_would_accept"):
        print(f"\n  *** SAVED annual report -> {SAVE_PATH} ({info['pages']} pages) ***")
        break
    elif info.get("ok"):
        print("  downloaded but Stage-1 rejected (0-1 SBC hits) -> try next candidate")
