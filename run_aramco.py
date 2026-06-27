"""One-off driver: resolve + download Saudi Aramco's latest annual report and SAVE the PDF."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv()
import ir_resolve_proto as R
import ir_fetch_proto as F

NAME, TICKER, COUNTRY = "Saudi Arabian Oil Company", "2222", "Saudi Arabia"
SAVE_PATH = "_ir_2222_Aramco.pdf"

print("=" * 70, "\nRESOLVE\n", "=" * 70)
res = R.resolve(NAME, TICKER, "", COUNTRY)
print(f"  chosen_url : {res.get('chosen_url')}")
print(f"  domain     : {res.get('registrable')}  backers={res.get('backers')}")
print(f"  confidence : {res['confidence']}   flags={res.get('flags')}")
for ev in res.get("evidence", []):
    extra = (f" fuzzy={ev['fuzzy']:.0f}" if "fuzzy" in ev
             else f" score={ev['score']:.0f}/runner={ev['runner_up']:.0f}" if "score" in ev else "")
    print(f"     [{ev['source']:9}] {ev['url']}{extra}")

ir_url = res.get("chosen_url")
print("\n", "=" * 70, "\nFETCH (newest annual report)\n", "=" * 70)
if not ir_url:
    print("  ABSTAIN at resolve — no IR url to fetch from")
    sys.exit(0)
result = F.fetch_annual_report(ir_url, allow_fc=True, save_path=SAVE_PATH)
if not result:
    print("  ABSTAIN — no gate-passing annual report found")
else:
    info = result["info"]
    print(f"\n  *** SAVED -> {SAVE_PATH}  (FY{result['fiscal_year']}, {info['pages']} pages) ***")
    print(f"      url={result['url']}")
    print(f"      sbc_hits={info['sbc_hits']}")
