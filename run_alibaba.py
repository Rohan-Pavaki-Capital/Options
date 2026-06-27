"""One-off driver: run the layered resolver + hybrid downloader on Alibaba (9988.HK)."""
import ir_resolve_proto as R
import ir_fetch_proto as F

NAME, TICKER, COUNTRY = "Alibaba Group Holding Limited", "9988", "Hong Kong"

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
else:
    print("  top candidates:")
    for sc, u, a in ranked[:6]:
        print(f"    [{sc:+.0f}] {a[:45]!r}  {u[:95]}")
    best_sc, best_url, _ = ranked[0]
    if best_sc <= 0:
        print("  best <=0 -> ABSTAIN")
    else:
        print(f"\n  downloading: {best_url}")
        info = F.inspect_pdf(best_url, ir_url)
        print(f"  inspect: {info}")
