"""
USA XBRL Options Extractor (comparison prototype)
==================================================

Extracts share-based compensation data for US-listed companies directly from
the XBRL facts of a SEC filing (10-K by default) — NO PDF render, NO OCR,
NO LLM. Numbers are the exact values the company tagged in its filing.

Purpose: side-by-side comparison against the existing PDF/LLM pipeline
(`/api/extract-from-edgar`). Output uses the SAME JSON schema
(Anthropic/schema.py) and the SAME Excel builder (format/json_to_excel.py),
so the two workbooks are directly comparable.

Endpoints (mounted by backend.py):
    POST /api/extract-from-xbrl        {"ticker": "MSFT", "form": "10-K"}
    GET  /api/xbrl/download/{filename} download the generated .xlsx / .json

Standalone CLI (no server needed):
    python "USA xbrl/xbrl_service.py" MSFT --form 10-K

Known limitations (by design — this is what the comparison should surface):
    - Narrative fields (vesting_description, performance_conditions, plan
      descriptions) are NOT in XBRL discrete facts -> stay null.
    - Per-plan breakdowns exist only when the filer tags the PlanNameAxis.
    - Exercise price range tables are usually TextBlock-only -> stay null.
    - Tranche/grant-level detail is not tagged in 10-K XBRL -> empty.
"""

from __future__ import annotations

import math
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

# Project root on sys.path so this file works standalone (CLI) and when
# loaded by backend.py via importlib (folder name contains a space, so it
# cannot be a normal package).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

OUTPUT_DIR = Path(__file__).resolve().parent / "output"


# ═════════════════════════════════════════════════════════════════════════
# EDGAR / XBRL FETCH
# ═════════════════════════════════════════════════════════════════════════

def _fetch_xbrl(ticker: str, form: str = "10-K"):
    """Fetch the latest filing's XBRL for a ticker. Returns (xbrl, meta)."""
    from markets.edgar_fetch import _ensure_identity, _normalize_ticker

    _ensure_identity()
    ticker = _normalize_ticker(ticker)

    from edgar import Company
    company = Company(ticker)
    if company is None:
        raise LookupError(f"No EDGAR company found for ticker {ticker!r}")

    filings = company.get_filings(form=form)
    if filings is None or len(filings) == 0:
        raise LookupError(f"No {form} filings found for {ticker}")
    filing = filings[0]

    try:
        xbrl = filing.xbrl()
    except Exception:
        from edgar.xbrl import XBRL
        xbrl = XBRL.from_filing(filing)
    if xbrl is None:
        raise LookupError(f"Filing {filing.accession_no} has no XBRL data")

    meta = {
        "ticker": ticker,
        "company": getattr(company, "name", None),
        "cik": getattr(company, "cik", None),
        "form": form,
        "accession": getattr(filing, "accession_no", None),
        "filing_date": str(getattr(filing, "filing_date", "") or ""),
        "url": getattr(filing, "filing_url", None) or getattr(filing, "url", None),
    }
    return xbrl, meta


# ═════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════

def _parse_date(s) -> Optional[date]:
    if s is None:
        return None
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    if isinstance(s, datetime):
        return s.date()
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _days_apart(a: Optional[date], b: Optional[date]) -> int:
    if a is None or b is None:
        return 10**9
    return abs((a - b).days)


_ISO_DUR_RE = re.compile(
    r"^P(?:(?P<y>\d+(?:\.\d+)?)Y)?(?:(?P<m>\d+(?:\.\d+)?)M)?(?:(?P<d>\d+(?:\.\d+)?)D)?$"
)


def _duration_to_years(value) -> Optional[float]:
    """'P4Y', 'P4Y6M', 'P548D' -> years. Plain numbers pass through as years."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        f = float(s)
        return round(f, 4) if not math.isnan(f) else None
    except ValueError:
        pass
    m = _ISO_DUR_RE.match(s.upper())
    if not m:
        return None
    y = float(m.group("y") or 0)
    mo = float(m.group("m") or 0)
    d = float(m.group("d") or 0)
    years = y + mo / 12.0 + d / 365.0
    return round(years, 4) if years > 0 else None


def _to_pct(value, unit_ref) -> Optional[float]:
    """XBRL percent facts are decimal fractions per spec (0.35 == 35%)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v):
        return None
    if str(unit_ref or "").lower() == "pure" and abs(v) <= 1.5:
        v *= 100.0
    return round(v, 6)


def _local_name(concept: str) -> str:
    """'us-gaap:ShareBased...' -> 'ShareBased...'"""
    return (concept or "").split(":")[-1]


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    # Keep integers clean (share counts)
    return int(f) if f == int(f) and abs(f) < 1e15 else f


# ── Award-type member -> (plan label, schema plan_type, is_nil_cost) ──────
_AWARD_TYPE_MAP = {
    "EmployeeStockOptionMember": ("Stock Options", "ESOP", False),
    "StockOptionMember": ("Stock Options", "ESOP", False),
    "RestrictedStockUnitsRSUMember": ("Restricted Stock Units", "RSU", True),
    "RestrictedStockUnitsMember": ("Restricted Stock Units", "RSU", True),
    "RestrictedStockMember": ("Restricted Stock", "RSP", True),
    "PerformanceSharesMember": ("Performance Share Units", "PSU", True),
    "PerformanceShareUnitsMember": ("Performance Share Units", "PSU", True),
    "EmployeeStockMember": ("Employee Stock Purchase Plan", "ESPP", False),
    "StockAppreciationRightsSARSMember": ("Stock Appreciation Rights", "SAR", False),
    "WarrantMember": ("Warrants", "WARRANT", False),
    "PhantomShareUnitsPSUsMember": ("Phantom Share Units", "OTHER", True),
}


def _prettify_member(member: str) -> str:
    """'aapl:SomeCustomPlanMember' -> 'Some Custom Plan'."""
    name = _local_name(member or "")
    name = re.sub(r"Member$", "", name)
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).strip() or name


# ═════════════════════════════════════════════════════════════════════════
# CONCEPT MAPPING (schema field <- us-gaap local-name regex)
# ═════════════════════════════════════════════════════════════════════════
# All regexes are matched case-insensitively against the concept local name
# ("ShareBased" vs "Sharebased" spelling varies inside the us-gaap taxonomy).

_OPT = {
    "outstanding_number": r"OptionsOutstandingNumber$",
    "outstanding_waep": r"OptionsOutstandingWeightedAverageExercisePrice$",
    "granted": r"OptionsGrantsInPeriod(Gross|Net)?$",
    "granted_waep": r"OptionsGrantsInPeriodWeightedAverageExercisePrice$",
    "granted_wagdfv": r"OptionsGrantsInPeriodWeightedAverageGrantDateFairValue$",
    "exercised": r"(OptionsExercisesInPeriod|StockIssuedDuringPeriodSharesStockOptionsExercised)$",
    "exercised_waep": r"OptionsExercisesInPeriodWeightedAverageExercisePrice$",
    "forfeited": r"OptionsForfeituresInPeriod$",
    "expired": r"OptionsExpirationsInPeriod$",
    "forfeited_and_expired": r"OptionsForfeituresAndExpirationsInPeriod(Gross)?$",
    "forfeited_waep": r"OptionsForfeituresInPeriodWeightedAverageExercisePrice$",
    "exercisable_number": r"OptionsExercisableNumber$",
    "exercisable_waep": r"OptionsExercisableWeightedAverageExercisePrice$",
    "remaining_term": r"OptionsOutstandingWeightedAverageRemainingContractualTerm\d?$",
}

_RSU = {
    "nonvested_number": r"EquityInstrumentsOtherThanOptionsNonvestedNumber$",
    "nonvested_wagdfv": r"EquityInstrumentsOtherThanOptionsNonvestedWeightedAverageGrantDateFairValue$",
    "granted": r"EquityInstrumentsOtherThanOptionsGrantsInPeriod(Gross)?$",
    "granted_wagdfv": r"EquityInstrumentsOtherThanOptionsGrantsInPeriodWeightedAverageGrantDateFairValue$",
    "vested": r"EquityInstrumentsOtherThanOptionsVestedInPeriod$",
    "vested_wagdfv": r"EquityInstrumentsOtherThanOptionsVestedInPeriodWeightedAverageGrantDateFairValue$",
    "forfeited": r"EquityInstrumentsOtherThanOptionsForfeit(ed|ures)InPeriod$",
    "forfeited_wagdfv": r"EquityInstrumentsOtherThanOptionsForfeituresWeightedAverageGrantDateFairValue$",
}

_SHARED = {
    "volatility": r"FairValueAssumptionsExpectedVolatilityRate$",
    "dividend_yield": r"FairValueAssumptionsExpectedDividendRate$",
    "risk_free_rate": r"FairValueAssumptionsRiskFreeInterestRate$",
    "expected_term": r"FairValueAssumptionsExpectedTerm1?$",
    "vesting_period": r"AwardVestingPeriod1?$",
    # Custom filer tags stating vesting as "N equal increments on the first N
    # anniversaries of the grant date" (e.g. ibm:...NumberOfAnniversariesOf
    # GrantDate = 4 -> vests fully after 4 years). Anniversaries are annual by
    # definition, so the count equals years. Used only when the standard
    # AwardVestingPeriod tag is absent.
    "vesting_anniversaries": r"AnniversariesOfGrantDate$",
    "expiration_period": r"AwardExpirationPeriod$",
    # Weighted-average period over which unrecognized compensation cost will
    # be recognized (e.g. CAG: "recognized over a weighted average period of
    # 1.7 years"). Disclosed per award type; proxies the REMAINING life of the
    # outstanding awards when no vesting period / WARCL is tagged.
    "expense_recognition_period":
        r"CompensationCostNotYetRecognizedPeriodForRecognition\d?$",
}

_ALL_PATTERNS = {**_OPT, **_RSU, **_SHARED}


def _classify_concept(local: str) -> Optional[tuple]:
    """Return (family, field) for a concept local name, else None.
    family: 'options' | 'rsu' | 'shared'.

    RSU concepts contain 'OtherThanOptions' but END with strings like
    'OptionsGrantsInPeriod', so they must be tested BEFORE the options
    patterns and excluded from them."""
    is_other_than_options = "otherthanoptions" in local.lower()
    if is_other_than_options:
        for field, pat in _RSU.items():
            if re.search(pat, local, re.IGNORECASE):
                return ("rsu", field)
    else:
        for field, pat in _OPT.items():
            if re.search(pat, local, re.IGNORECASE):
                return ("options", field)
    for field, pat in _SHARED.items():
        if re.search(pat, local, re.IGNORECASE):
            return ("shared", field)
    return None


# ═════════════════════════════════════════════════════════════════════════
# CORE EXTRACTION
# ═════════════════════════════════════════════════════════════════════════

def extract_options_from_xbrl(xbrl, log=None) -> dict:
    """Map the filing's XBRL facts into the pipeline's OUTPUT_SCHEMA dict.

    `log`: optional callable(str) for step-by-step diagnostics (used by the
    /api/xbrl/excel/options endpoint); silent when None."""
    log = log or (lambda m: None)
    df = xbrl.query(include_dimensions=True).to_dataframe()
    if df is None or len(df) == 0:
        raise ValueError("XBRL document contains no facts")

    por = _parse_date(getattr(xbrl, "period_of_report", None))
    log(f"XBRL loaded: {len(df)} facts, period_of_report={por}, "
        f"entity={getattr(xbrl, 'entity_name', '?')}")

    # ── Identify the current reporting duration and matching instants ──
    # The current duration MUST end at the filing's period-of-report: annual
    # for a 10-K, year-to-date for a 10-Q (longest wins over the lone quarter).
    # NO fallback to older periods — a 10-Q that only carries prior-FY
    # comparatives must fail here rather than emit stale balances relabeled
    # as current data.
    dur = df[df["period_start"].notna() & df["period_end"].notna()][
        ["period_start", "period_end"]
    ].drop_duplicates()
    periods = [
        (s, e)
        for s, e in ((_parse_date(r.period_start), _parse_date(r.period_end))
                     for r in dur.itertuples())
        if s and e and 20 <= (e - s).days <= 400
    ]
    candidates = [p for p in periods if _days_apart(p[1], por) <= 15]
    if not candidates:
        raise ValueError(
            "No reporting duration in this filing's XBRL ends at its "
            f"period of report ({por}) — cannot extract current-period data"
        )
    cur = max(candidates, key=lambda p: (p[1] - p[0]).days)
    cur_start, cur_end = cur
    cur_len = (cur_end - cur_start).days
    log(f"period selection: current={cur_start}..{cur_end} ({cur_len}d) "
        f"from {len(candidates)} candidate(s) ending at period-of-report")
    # Prior period: comparable length, ending where the current one starts.
    pri = min(
        (p for p in periods
         if _days_apart(p[1], cur_start) <= 15
         and abs((p[1] - p[0]).days - cur_len) <= max(30, cur_len // 5)),
        key=lambda p: _days_apart(p[1], cur_start),
        default=None,
    )
    log(f"period selection: prior={pri[0]}..{pri[1]}" if pri
        else "period selection: no comparable prior period found")

    dim_cols = [c for c in df.columns if c.startswith("dim_")]
    award_col = next((c for c in dim_cols if c.endswith("AwardTypeAxis")), None)
    plan_col = next((c for c in dim_cols if c.endswith("PlanNameAxis")), None)
    other_dim_cols = [c for c in dim_cols if c not in (award_col, plan_col)]

    def _isna(v) -> bool:
        return v is None or (isinstance(v, float) and math.isnan(v)) or str(v) == "nan"

    # ── Collect relevant facts into buckets ────────────────────────────
    # bucket key: (family, award_member, plan_member) -> {field: {slot: value}}
    buckets: dict[tuple, dict] = {}
    currencies: list[str] = []
    # Shared-field facts carrying EXTRA axes (e.g. a vesting period tagged per
    # grant-year cohort via AwardDateAxis). Not trusted directly — used only
    # when no clean fact exists AND every cohort discloses the SAME value.
    aux_shared: dict[tuple, list] = {}

    # NOTE: dict records, not itertuples() — dimension column names contain
    # '-' / '.' which itertuples() silently renames, breaking getattr access.
    for row in df.to_dict("records"):
        local = _local_name(row.get("concept") or "")
        cls = _classify_concept(local)
        if cls is None:
            continue
        family, field = cls

        # Facts carrying extra axes (Range min/max, equity components, grant-
        # year cohorts...) are NOT headline rollforward values. Rollforward
        # facts with extra axes are skipped outright. TWO exceptions, both
        # parked in aux_shared and used only if unanimous and no clean fact
        # exists for the bucket:
        #   - shared facts sliced ONLY by grant-year cohort (AwardDateAxis)
        #   - VESTING facts tagged as a min/max range: the MAXIMUM bound is
        #     the full vesting period under graded vesting (e.g. IBM RSUs:
        #     min P1Y / max P4Y = tranches vest years 1-4, fully vested at 4).
        #     Minimum bounds and ranges on any other field are never used.
        extra_dims = [c for c in other_dim_cols if not _isna(row.get(c))]
        if extra_dims:
            if family != "shared":
                continue

            def _extra_dim_ok(c):
                if c.endswith("AwardDateAxis"):
                    return True
                if (field in ("vesting_period", "vesting_anniversaries")
                        and c.endswith("RangeAxis")):
                    return str(row.get(c)).endswith("MaximumMember")
                return False

            if any(not _extra_dim_ok(c) for c in extra_dims):
                continue
        has_extra_dims = bool(extra_dims)

        award = row.get(award_col) if award_col else None
        plan = row.get(plan_col) if plan_col else None
        award = None if _isna(award) else str(award)
        plan = None if _isna(plan) else str(plan)

        # Time slot
        ps, pe = _parse_date(row.get("period_start")), _parse_date(row.get("period_end"))
        pi = _parse_date(row.get("period_instant"))
        if pi is not None:
            if _days_apart(pi, cur_end) <= 7:
                slot = "closing"
            elif _days_apart(pi, cur_start) <= 10:
                slot = "opening"
            elif pri and _days_apart(pi, pri[0]) <= 10:
                slot = "prior_opening"
            else:
                continue
        elif ps is not None and pe is not None:
            if _days_apart(pe, cur_end) <= 15 and _days_apart(ps, cur_start) <= 15:
                slot = "current"
            elif pri and _days_apart(pe, pri[1]) <= 15 and _days_apart(ps, pri[0]) <= 15:
                slot = "prior"
            else:
                continue
        else:
            continue

        # Value: numeric when available, else raw (ISO durations)
        val = _num(row.get("numeric_value"))
        if val is None:
            raw = row.get("value")
            val = None if _isna(raw) else raw
        if val is None:
            continue

        unit = row.get("unit_ref")
        ccy = row.get("currency")
        if not _isna(ccy):
            currencies.append(str(ccy))

        key = (family, award, plan)
        if has_extra_dims:
            aux_shared.setdefault((key, field, slot), []).append((val, unit))
            continue
        slots = buckets.setdefault(key, {}).setdefault(field, {})
        # First value wins per (field, slot) — dedupes repeats across statements
        slots.setdefault(slot, (val, unit))

    # Promote cohort-dimensioned shared facts ONLY when every cohort discloses
    # the identical value and no clean (undimensioned) fact already exists.
    for (key, field, slot), pairs in aux_shared.items():
        if len({str(v) for v, _u in pairs}) != 1:
            log(f"aux fact DROPPED (cohorts disagree): {field}/{slot} "
                f"on {key[1] or 'no-award'} values={[str(v) for v, _u in pairs]}")
            continue
        slots = buckets.setdefault(key, {}).setdefault(field, {})
        slots.setdefault(slot, pairs[0])
        log(f"aux fact promoted (unanimous): {field}/{slot}="
            f"{pairs[0][0]} on {key[1] or 'no-award'}")

    if not buckets:
        raise ValueError(
            "No share-based compensation facts found in this filing's XBRL"
        )
    for k in sorted(buckets, key=str):
        log(f"bucket [{k[0]}] award={_local_name(k[1] or '') or '-'} "
            f"plan={_local_name(k[2] or '') or '-'}: "
            f"fields={sorted(buckets[k].keys())}")

    # ── Merge detail-only buckets into their family's core bucket ──────
    # Some filers (e.g. IBM) tag the rollforward UNDIMENSIONED but the detail
    # facts (WARCL, prices) per award type — or vice versa. A bucket with no
    # core rollforward numbers is a "detail" bucket; fold it into the core
    # bucket of the same family when the award member unambiguously belongs
    # to that family (standard us-gaap members only — custom members like a
    # filer's SAR variants must never leak into another plan).
    _CORE_FIELDS = {"outstanding_number", "granted", "exercised", "forfeited",
                    "expired", "forfeited_and_expired", "exercisable_number",
                    "nonvested_number", "vested"}
    _OPT_MEMBERS = {"EmployeeStockOptionMember", "StockOptionMember"}
    _RSU_TYPES = {"RSU", "PSU", "RSP"}
    # Standard award member -> name stem for matching a filer's CUSTOM
    # sub-classified members (GIS: RestrictedStockUnitEquityClassifiedMember
    # is still a restricted stock unit — the standard RSU vesting fact
    # describes it too).
    _AWARD_STEMS = {
        "EmployeeStockOptionMember": "StockOption",
        "StockOptionMember": "StockOption",
        "RestrictedStockUnitsRSUMember": "RestrictedStockUnit",
        "RestrictedStockUnitsMember": "RestrictedStockUnit",
        "RestrictedStockMember": "RestrictedStock",
        "PerformanceSharesMember": "PerformanceShare",
        "PerformanceShareUnitsMember": "PerformanceShare",
    }

    def _stem_matches(stem: str, member) -> bool:
        name = _local_name(member or "")
        if stem not in name:
            return False
        # "RestrictedStock" must not swallow "RestrictedStockUnit..." members
        if stem == "RestrictedStock" and "RestrictedStockUnit" in name:
            return False
        return True

    def _family_of_award(award) -> Optional[str]:
        aloc = _local_name(award or "")
        if aloc in _OPT_MEMBERS:
            return "options"
        mapped = _AWARD_TYPE_MAP.get(aloc)
        if mapped is not None and mapped[1] in _RSU_TYPES:
            return "rsu"
        return None

    core_keys = [k for k in buckets
                 if k[0] != "shared"
                 and any(f in buckets[k] for f in _CORE_FIELDS)]
    detail_keys = [k for k in buckets
                   if k[0] != "shared" and k not in core_keys]
    for dkey in detail_keys:
        fam, award, plan = dkey
        if award is not None and _family_of_award(award) != fam:
            continue
        fam_cores = [k for k in core_keys if k[0] == fam]
        target = next(
            (k for k in fam_cores if k[1] == award and k[2] == plan), None)
        if target is None and award is not None:
            target = next((k for k in fam_cores if k[1] == award), None)
        if target is None and len(fam_cores) == 1:
            target = fam_cores[0]
        if target is None:
            log(f"detail bucket UNMATCHED (kept separate/dropped): "
                f"award={_local_name(dkey[1] or '') or '-'} "
                f"fields={sorted(buckets[dkey].keys())}")
            continue
        log(f"detail bucket merged: award={_local_name(dkey[1] or '') or '-'} "
            f"fields={sorted(buckets[dkey].keys())} -> "
            f"[{target[0]}] award={_local_name(target[1] or '') or '-'}")
        for field, slots in buckets[dkey].items():
            tslots = buckets[target].setdefault(field, {})
            for s, v in slots.items():
                tslots.setdefault(s, v)
        del buckets[dkey]

    # ── Merge 'shared' buckets into their sibling options/rsu bucket ───
    shared_keys = [k for k in buckets if k[0] == "shared"]
    real_keys = [k for k in buckets if k[0] != "shared"]
    for skey in shared_keys:
        _, award, plan = skey
        targets: list = []
        if award or plan:
            # Dimensioned shared facts attach ONLY to bucket(s) for the same
            # award type (+plan). No cross-bucket guessing: a fact tagged for
            # e.g. a custom SAR member must never land on the options plan.
            t = next(
                (k for k in real_keys if k[1] == award and k[2] == plan), None
            )
            if t is None and award:
                t = next((k for k in real_keys if k[1] == award), None)
            if t is None and award:
                # Standard us-gaap award member vs an undimensioned core
                # bucket of the same family (guarded like the detail merge).
                fam = _family_of_award(award)
                if fam is not None:
                    fam_cores = [k for k in real_keys if k[0] == fam]
                    if len(fam_cores) == 1:
                        t = fam_cores[0]
            if t is not None:
                targets = [t]
            elif award:
                # Standard award member vs a filer's custom SUB-CLASSIFIED
                # members of the same category (GIS: RSU vesting tagged on
                # RestrictedStockUnitsRSUMember; rollforwards on
                # RestrictedStockUnit{Equity,Liability}ClassifiedMember).
                # The category-level disclosure applies to ALL of them.
                stem = _AWARD_STEMS.get(_local_name(award))
                if stem:
                    targets = [k for k in real_keys
                               if _stem_matches(stem, k[1])]
        else:
            # Undimensioned assumptions: attach to the options bucket if there
            # is exactly one, else to the single bucket overall, else drop.
            opts = [k for k in real_keys if k[0] == "options"]
            if len(opts) == 1:
                targets = [opts[0]]
            elif len(real_keys) == 1:
                targets = [real_keys[0]]
        if targets:
            for target in targets:
                log(f"shared facts attached: {sorted(buckets[skey].keys())} "
                    f"(award={_local_name(award or '') or '-'}) -> "
                    f"[{target[0]}] award={_local_name(target[1] or '') or '-'}")
                for field, slots in buckets[skey].items():
                    buckets[target].setdefault(field, {}).update(
                        {s: v for s, v in slots.items()
                         if s not in buckets[target].get(field, {})}
                    )
        else:
            log(f"shared facts DROPPED (no safe target): "
                f"{sorted(buckets[skey].keys())} "
                f"(award={_local_name(award or '') or '-'})")
        del buckets[skey]

    # ── Build schema plans ──────────────────────────────────────────────
    ccy = max(set(currencies), key=currencies.count) if currencies else "USD"
    price_unit = "dollars" if ccy == "USD" else ccy.lower()

    def g(fields, name, slot):
        pair = fields.get(name, {}).get(slot)
        return pair[0] if pair else None

    def gu(fields, name, slot):
        pair = fields.get(name, {}).get(slot)
        return pair if pair else (None, None)

    plans = []
    for (family, award, plan_member), fields in sorted(
        buckets.items(), key=lambda kv: (kv[0][0] != "options", str(kv[0][1]))
    ):
        label, ptype, nil_cost = None, None, None
        if award:
            mapped = _AWARD_TYPE_MAP.get(_local_name(award))
            if mapped:
                label, ptype, nil_cost = mapped
            else:
                label = _prettify_member(award)
        if label is None:
            label = "Stock Options" if family == "options" else "Share Awards"
        if plan_member:
            label = f"{_prettify_member(plan_member)} — {label}"
        if ptype is None:
            ptype = "ESOP" if family == "options" else "RSU"
        if nil_cost is None:
            nil_cost = family != "options"

        p: dict[str, Any] = {
            "plan_name": label,
            "plan_type": ptype,
            "plan_description": None,
            "is_cash_settled": None,
            "is_nil_cost": nil_cost,
            "units_label": None,
        }

        def flow(v):
            n = _num(v)
            return abs(n) if isinstance(n, (int, float)) else n

        if family == "options":
            forfeited = flow(g(fields, "forfeited", "current"))
            expired = flow(g(fields, "expired", "current"))
            combined = flow(g(fields, "forfeited_and_expired", "current"))
            forf = combined if combined is not None else (
                None if forfeited is None and expired is None
                else (forfeited or 0) + (expired or 0)
            )
            p.update({
                "opening_balance": g(fields, "outstanding_number", "opening"),
                "granted": flow(g(fields, "granted", "current")),
                "exercised": flow(g(fields, "exercised", "current")),
                "forfeited_or_lapsed": forf,
                "closing_balance": g(fields, "outstanding_number", "closing"),
                "exercisable_at_period_end": g(fields, "exercisable_number", "closing"),
                "weighted_avg_exercise_price": g(fields, "outstanding_waep", "closing"),
                "weighted_avg_exercise_price_unit":
                    price_unit if g(fields, "outstanding_waep", "closing") is not None else None,
                "weighted_avg_grant_date_fair_value": g(fields, "granted_wagdfv", "current"),
                "fair_value_unit":
                    price_unit if g(fields, "granted_wagdfv", "current") is not None else None,
                # Filers tag WARCL either as an instant at period end or in
                # the fiscal-period duration context — accept both.
                "weighted_avg_remaining_contractual_life_years":
                    _duration_to_years(g(fields, "remaining_term", "closing")
                                       or g(fields, "remaining_term", "current")),
            })
        else:
            # WAGDFV must pair with the CLOSING balance -> use the nonvested-
            # at-period-end fair value (same convention as options' closing
            # WAEP), NOT the grants-in-period fair value. The grants-in-period
            # value is still surfaced via valuation_inputs.fair_value_per_option.
            p.update({
                "opening_balance": g(fields, "nonvested_number", "opening"),
                "granted": flow(g(fields, "granted", "current")),
                "vested": flow(g(fields, "vested", "current")),
                "forfeited_or_lapsed": flow(g(fields, "forfeited", "current")),
                "closing_balance": g(fields, "nonvested_number", "closing"),
                "weighted_avg_grant_date_fair_value": g(fields, "nonvested_wagdfv", "closing"),
                "fair_value_unit":
                    price_unit if g(fields, "nonvested_wagdfv", "closing") is not None else None,
            })

        p["vesting_period_years"] = _duration_to_years(
            g(fields, "vesting_period", "current")
            or g(fields, "vesting_period", "closing")
            or g(fields, "vesting_anniversaries", "current")
            or g(fields, "vesting_anniversaries", "closing")
        )
        # Remaining life of the outstanding awards, per the filer's own
        # disclosure of the unrecognized-cost recognition period.
        p["remaining_expense_recognition_years"] = _duration_to_years(
            g(fields, "expense_recognition_period", "current")
            or g(fields, "expense_recognition_period", "closing")
        )

        vol, vol_u = gu(fields, "volatility", "current")
        div, div_u = gu(fields, "dividend_yield", "current")
        rf, rf_u = gu(fields, "risk_free_rate", "current")
        term = _duration_to_years(g(fields, "expected_term", "current"))
        exp_years = _duration_to_years(
            g(fields, "expiration_period", "current")
            or g(fields, "expiration_period", "closing")
        )
        # Grants-in-period fair value (per option/unit granted this year).
        fv = g(fields, "granted_wagdfv", "current")
        if any(v is not None for v in (vol, div, rf, term, exp_years, fv)):
            p["valuation_inputs"] = {
                "expiration_years": exp_years,
                "expected_life_years": term,
                "volatility_pct": _to_pct(vol, vol_u),
                "dividend_yield_pct": _to_pct(div, div_u),
                "risk_free_rate_pct": _to_pct(rf, rf_u),
                "fair_value_per_option": fv,
                "fair_value_unit": price_unit if fv is not None else None,
            }

        # ── prior year ──
        prior: dict[str, Any] = {}
        if family == "options":
            pf = flow(g(fields, "forfeited", "prior"))
            pe_ = flow(g(fields, "expired", "prior"))
            pc = flow(g(fields, "forfeited_and_expired", "prior"))
            prior = {
                "opening_balance": g(fields, "outstanding_number", "prior_opening"),
                "granted": flow(g(fields, "granted", "prior")),
                "exercised": flow(g(fields, "exercised", "prior")),
                "forfeited_or_lapsed": pc if pc is not None else (
                    None if pf is None and pe_ is None else (pf or 0) + (pe_ or 0)
                ),
                "closing_balance": g(fields, "outstanding_number", "opening"),
                "weighted_avg_exercise_price": g(fields, "outstanding_waep", "opening"),
                "weighted_avg_exercise_price_unit":
                    price_unit if g(fields, "outstanding_waep", "opening") is not None else None,
                "weighted_avg_grant_date_fair_value": g(fields, "granted_wagdfv", "prior"),
            }
        else:
            prior = {
                "opening_balance": g(fields, "nonvested_number", "prior_opening"),
                "granted": flow(g(fields, "granted", "prior")),
                "vested": flow(g(fields, "vested", "prior")),
                "forfeited_or_lapsed": flow(g(fields, "forfeited", "prior")),
                "closing_balance": g(fields, "nonvested_number", "opening"),
                # Pairs with the prior CLOSING balance (= this year's opening).
                "weighted_avg_grant_date_fair_value": g(fields, "nonvested_wagdfv", "opening"),
            }
        prior = {k: v for k, v in prior.items() if v is not None}
        if prior:
            p["prior_year"] = prior

        # Drop plans where XBRL had no actual rollforward numbers
        core = ["opening_balance", "granted", "exercised", "vested",
                "forfeited_or_lapsed", "closing_balance",
                "exercisable_at_period_end"]
        if all(p.get(k) is None for k in core):
            log(f"plan dropped (no rollforward numbers): {p['plan_name']}")
            continue

        log(f"plan built: {p['plan_name']} [{p['plan_type']}] "
            f"opening={p.get('opening_balance')} granted={p.get('granted')} "
            f"closing={p.get('closing_balance')} "
            f"waep={p.get('weighted_avg_exercise_price')} "
            f"wagdfv={p.get('weighted_avg_grant_date_fair_value')} "
            f"vesting={p.get('vesting_period_years')} "
            f"warcl={p.get('weighted_avg_remaining_contractual_life_years')} "
            f"recognition={p.get('remaining_expense_recognition_years')}")
        plans.append({k: v for k, v in p.items() if v is not None or k in (
            "plan_name", "plan_type")})

    return {
        "company_name": getattr(xbrl, "entity_name", None),
        "report_period": str(getattr(xbrl, "period_of_report", "") or "") or None,
        "currency": ccy,
        "reporting_standard": "US_GAAP",
        "plans": plans,
    }


def _coverage(extraction: dict) -> dict:
    """Which schema fields did XBRL fill vs leave empty, per plan."""
    NARRATIVE = [
        "vesting_description", "performance_conditions", "plan_description",
        "exercise_price_range_low", "exercise_price_range_high",
        "weighted_avg_share_price_at_exercise", "tranches",
    ]
    out = []
    for p in extraction.get("plans", []):
        filled = sorted(
            k for k, v in p.items()
            if v is not None and k not in ("plan_name", "plan_type", "prior_year")
        )
        out.append({
            "plan_name": p.get("plan_name"),
            "fields_filled": filled,
            "known_gaps_vs_pdf": [k for k in NARRATIVE if p.get(k) is None],
            "has_prior_year": "prior_year" in p,
        })
    return {"plans": out, "plan_count": len(out)}


def _map_plans_to_excel_strict(extraction: dict, log=None) -> list[dict]:
    """Workbook rows in the /api/excel/options format but with NO fallback
    values: every field is either the exact disclosed XBRL value or null.

    Differences vs core/excel_options.map_plans_to_excel (used by the PDF
    endpoint, deliberately untouched):
        - maturity_years: real value or null — never the 4.0 default
        - RSU strike: grant-date fair value or null — never the 0.1 floor
    maturity_years chain (every link is a DISCLOSED fact, never a constant).
    REMAINING-LIFE-FIRST per user decision (2026-07-09, supersedes the
    earlier vesting-first choice): maturity means the remaining life of the
    outstanding options, the standard input for valuing them.
        option: remaining contractual life -> vesting period
                -> unrecognized-cost recognition period
        rsu:    vesting period -> unrecognized-cost recognition period
    Selection logic is otherwise the same: a row needs a positive closing
    balance, kind is option/rsu, top 3 by count_mn descending."""
    from core.excel_options import _to_float, _to_millions

    log = log or (lambda m: None)
    plans = extraction.get("plans") if isinstance(extraction, dict) else None
    if not isinstance(plans, list):
        return []

    def _first(plan, chain):
        """First non-null value in the chain; returns (value, source_field)."""
        for field in chain:
            v = _to_float(plan.get(field))
            if v is not None:
                return v, field
        return None, "none disclosed"

    out = []
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        name = plan.get("plan_name", "?")
        count_mn = _to_millions(plan.get("closing_balance"), plan.get("units_label"))
        if count_mn is None or count_mn <= 0:
            log(f"reducer: SKIP {name!r} — no positive closing balance")
            continue

        is_nil = plan.get("is_nil_cost")
        waep = _to_float(plan.get("weighted_avg_exercise_price"))

        if waep is not None and is_nil is False:
            maturity, src = _first(plan, (
                "weighted_avg_remaining_contractual_life_years",
                "vesting_period_years",
                "remaining_expense_recognition_years"))
            log(f"reducer: {name!r} -> option count_mn={count_mn} "
                f"strike={waep} (closing WAEP) maturity={maturity} ({src})")
            out.append({"count_mn": count_mn, "strike": waep,
                        "maturity_years": maturity, "kind": "option"})
        elif is_nil is True:
            maturity, src = _first(plan, (
                "vesting_period_years",
                "remaining_expense_recognition_years"))
            strike = _to_float(plan.get("weighted_avg_grant_date_fair_value"))
            log(f"reducer: {name!r} -> rsu count_mn={count_mn} "
                f"strike={strike} (nonvested closing WAGDFV) "
                f"maturity={maturity} ({src})")
            out.append({"count_mn": count_mn, "strike": strike,
                        "maturity_years": maturity, "kind": "rsu"})
        else:
            log(f"reducer: SKIP {name!r} — neither option (WAEP present, "
                f"not nil-cost) nor nil-cost award")

    out.sort(key=lambda p: p["count_mn"], reverse=True)
    if len(out) > 3:
        log(f"reducer: top 3 by count kept, {len(out) - 3} plan(s) trimmed")
    return out[:3]


# ═════════════════════════════════════════════════════════════════════════
# AI ASSEMBLY MODE (?mode=ai) — EXACT SAME LOGIC AS THE PDF WORKFLOW
# ═════════════════════════════════════════════════════════════════════════
# Per user decision (2026-07-09): mode=ai reuses the PDF pipeline's AI
# machinery UNCHANGED — Anthropic.extract_with_claude (same system/
# extraction/validation prompts, same OUTPUT_SCHEMA, same two-pass extract+
# validate and rollforward checks) followed by the PDF endpoint's exact
# reducer core.excel_options.map_plans_to_excel (INCLUDING its 4.0 maturity
# default and 0.1 strike floor). The only difference is the input: instead
# of rendered PDF pages, the LLM reads the filing's share-based-comp
# footnote text sourced from the XBRL TextBlocks (no Playwright, no OCR).

_AI_MODEL = "claude-sonnet-4-6"  # same model core/options.py passes

_AI_CONCEPT_RE = re.compile(
    r"ShareBasedCompensation|SharebasedCompensation|ShareBasedPayment"
    r"|CompensationRelatedCosts"
    r"|EquityInstrumentsOtherThanOptions|StockOption"
    r"|AwardVestingPeriod|AnniversariesOfGrantDate|PeriodForRecognition",
    re.IGNORECASE,
)

def _strip_html(html: str) -> str:
    txt = re.sub(r"</tr>", "\n", html)
    txt = re.sub(r"</td>", " | ", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = re.sub(r"&#160;|&nbsp;", " ", txt)
    return re.sub(r"[ \t]+", " ", txt)


def _collect_ai_inputs(xbrl):
    """Build (facts, prose). facts = list of dicts with id/concept/value/
    unit/period/dims for every SBC-related non-TextBlock fact."""
    df = xbrl.query(include_dimensions=True).to_dataframe()
    if df is None or len(df) == 0:
        raise ValueError("XBRL document contains no facts")

    dim_cols = [c for c in df.columns if c.startswith("dim_")]

    def _isna(v):
        return v is None or (isinstance(v, float) and math.isnan(v)) or str(v) == "nan"

    facts, prose_parts = [], []
    for row in df.to_dict("records"):
        concept = str(row.get("concept") or "")
        local = _local_name(concept)
        if not _AI_CONCEPT_RE.search(local):
            continue
        val = row.get("value")
        if _isna(val):
            continue
        sval = str(val)
        if "TextBlock" in local or sval.lstrip().startswith("<"):
            txt = _strip_html(sval)
            # User rule (2026-07-09): ONLY the notes-section share-based
            # compensation disclosure counts. The Item 12 "EQUITY
            # COMPENSATION PLAN INFORMATION" table (Part III — securities
            # AUTHORIZED under plans, not the rollforward) must be ignored.
            if re.search(r"equity\s+compensation\s+plan\s+information",
                         txt, re.IGNORECASE):
                continue
            prose_parts.append(txt)
            continue
        num = _num(row.get("numeric_value"))
        pi = row.get("period_instant")
        period = (str(pi)[:10] if not _isna(pi) else
                  f"{str(row.get('period_start'))[:10]}..{str(row.get('period_end'))[:10]}")
        dims = {c.split("_")[-1]: _local_name(str(row.get(c)))
                for c in dim_cols if not _isna(row.get(c))}
        facts.append({
            "id": f"F{len(facts) + 1}",
            "concept": local,
            "value": num if num is not None else sval,
            "unit": None if _isna(row.get("unit_ref")) else str(row.get("unit_ref")),
            "period": period,
            "dims": dims,
        })
        if len(facts) >= 400:
            break

    prose = re.sub(r"\n{3,}", "\n\n", "\n\n".join(prose_parts)).strip()[:30000]
    return facts, prose


def _ai_assemble_plans(xbrl, log):
    """EXACT same AI logic as the PDF workflow (user decision 2026-07-09):
    Anthropic.extract_with_claude — identical prompts, schema and two-pass
    extract+validate — reads the share-based-comp footnote (sourced from the
    XBRL TextBlocks instead of rendered PDF pages), then the SAME reducer as
    GET /api/excel/options (core.excel_options.map_plans_to_excel, including
    its 4.0 maturity default and 0.1 strike floor).
    Returns (workbook_rows, currency)."""
    import os as _os

    facts, prose = _collect_ai_inputs(xbrl)
    por = _parse_date(getattr(xbrl, "period_of_report", None))

    # No closing share counts at all -> pointless LLM call; let the caller
    # fall through to the next form (mirrors the PDF flow, whose page
    # detection also aborts before any LLM spend).
    count_re = re.compile(r"OptionsOutstandingNumber$|NonvestedNumber$", re.IGNORECASE)
    has_counts = any(
        count_re.search(f["concept"])
        and ".." not in f["period"]
        and _days_apart(_parse_date(f["period"]), por) <= 15
        for f in facts
    )
    if not has_counts:
        raise ValueError("no closing share-count facts at the period of report")
    if not prose:
        raise ValueError(
            "no share-based compensation footnote text in this filing's XBRL")

    api_key = _os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — mode=ai unavailable")
    import anthropic
    from Anthropic import extract_with_claude, validate_all_plans
    from core.excel_options import map_plans_to_excel

    log(f"ai: extracting from footnote text ({len(prose)} chars) with the "
        f"PDF workflow's prompts ({_AI_MODEL}, two-pass extract+validate)")
    client = anthropic.Anthropic(api_key=api_key)
    result = extract_with_claude(
        client, {1: prose}, {}, model=_AI_MODEL, use_vision=False,
    )
    if not isinstance(result, dict) or result.get("error"):
        detail = str((result or {}).get("details") or "")[:200]
        raise ValueError(
            f"extraction failed: {(result or {}).get('error')}: {detail}".rstrip(": "))
    result = validate_all_plans(result)

    for p in result.get("plans", []):
        log(f"ai: extracted plan {p.get('plan_name')!r} [{p.get('plan_type')}] "
            f"closing={p.get('closing_balance')} "
            f"waep={p.get('weighted_avg_exercise_price')} "
            f"wagdfv={p.get('weighted_avg_grant_date_fair_value')} "
            f"warcl={p.get('weighted_avg_remaining_contractual_life_years')} "
            f"vesting={p.get('vesting_period_years')}")

    plans = map_plans_to_excel(result)
    log(f"ai: PDF reducer emitted {len(plans)} plan(s) "
        f"(chain: WARCL -> vesting -> 4.0 default; RSU strike floor 0.1)")
    return plans, result.get("currency")


# ═════════════════════════════════════════════════════════════════════════
# IR-WEBSITE FALLBACK (user rule 2026-07-09)
# ═════════════════════════════════════════════════════════════════════════
# When NEITHER the 10-Q NOR the 10-K yields any options data, resolve the
# company's own IR website, download its latest ANNUAL REPORT PDF, and run
# the SAME PDF workflow on it: Stage 1/2 page detection -> Anthropic
# two-pass extraction -> PDF reducer.

_IR_MAX_PAGES = 12          # cap LLM spend on huge glossy annual reports
_IR_PAGES_PER_CALL = 5


def _ir_fallback_plans(ticker: str, log):
    """IR annual report -> PDF workflow -> workbook rows.
    Returns (workbook_rows, currency)."""
    import os as _os

    from routes.diamond_route import _attempt_irscraper
    from core.options import (detect_relevant_pages, extract_text_from_pages,
                              rasterize_pages)
    from Anthropic import extract_with_claude, merge_results, validate_all_plans
    from core.excel_options import map_plans_to_excel

    api_key = _os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — IR fallback unavailable")

    # Company name for the IR resolver (EDGAR knows it; fall back to ticker).
    name = ticker
    try:
        from markets.edgar_fetch import _ensure_identity
        _ensure_identity()
        from edgar import Company
        name = getattr(Company(ticker), "name", None) or ticker
    except Exception:
        pass

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = OUTPUT_DIR / f"{ticker}_ir_annual.pdf"
    log(f"ir-fallback: resolving IR site for {name!r} ({ticker})")
    info = _attempt_irscraper(name, ticker, pdf_path, "annual",
                              lambda done, total: None,
                              country="united states")
    log(f"ir-fallback: got annual report from {info.get('ir_url')} "
        f"(period {info.get('report_period')}, {info.get('pages')} pages)")

    # Stage 1/2 — same page detection as the PDF pipeline
    together_client = None
    together_key = _os.environ.get("TOGETHER_API_KEY")
    if together_key:
        try:
            from openai import OpenAI
            together_client = OpenAI(api_key=together_key,
                                     base_url="https://api.together.xyz/v1")
        except Exception:
            together_client = None
    pages, _cls = detect_relevant_pages(str(pdf_path),
                                        together_client=together_client)
    if not pages:
        raise ValueError("IR annual report has no share-based-comp pages")
    if len(pages) > _IR_MAX_PAGES:
        log(f"ir-fallback: {len(pages)} relevant pages, capping to first "
            f"{_IR_MAX_PAGES}")
        pages = pages[:_IR_MAX_PAGES]
    log(f"ir-fallback: extracting pages {pages}")

    texts = extract_text_from_pages(str(pdf_path), pages)
    images = rasterize_pages(str(pdf_path), pages)

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    results = []
    for i in range(0, len(pages), _IR_PAGES_PER_CALL):
        batch = pages[i:i + _IR_PAGES_PER_CALL]
        bt = {pg: texts[pg] for pg in batch if pg in texts}
        bi = {pg: images[pg] for pg in batch if pg in images}
        results.append(extract_with_claude(client, bt, bi, model=_AI_MODEL))

    result = validate_all_plans(merge_results(results))
    for p in result.get("plans", []):
        log(f"ir-fallback: extracted plan {p.get('plan_name')!r} "
            f"[{p.get('plan_type')}] closing={p.get('closing_balance')} "
            f"waep={p.get('weighted_avg_exercise_price')} "
            f"vesting={p.get('vesting_period_years')}")
    plans = map_plans_to_excel(result)
    log(f"ir-fallback: PDF reducer emitted {len(plans)} plan(s)")
    return plans, result.get("currency")


# ═════════════════════════════════════════════════════════════════════════
# ONE-CALL PIPELINE: ticker -> JSON + Excel on disk
# ═════════════════════════════════════════════════════════════════════════

def run_xbrl_extraction(ticker: str, form: str = "10-K") -> dict:
    import json as _json
    from format.json_to_excel import build_workbook

    xbrl, meta = _fetch_xbrl(ticker, form)
    extraction = extract_options_from_xbrl(xbrl)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{meta['ticker']}_{form.replace('/', '-')}_xbrl"
    json_path = OUTPUT_DIR / f"{stem}.json"
    xlsx_path = OUTPUT_DIR / f"{stem}_options.xlsx"

    json_path.write_text(
        _json.dumps(extraction, indent=2, default=str), encoding="utf-8"
    )
    build_workbook(str(json_path), str(xlsx_path))

    return {
        "meta": meta,
        "extraction": extraction,
        "coverage": _coverage(extraction),
        "json_file": json_path.name,
        "excel_file": xlsx_path.name,
    }


# ═════════════════════════════════════════════════════════════════════════
# FASTAPI ROUTER (mounted by backend.py)
# ═════════════════════════════════════════════════════════════════════════

try:
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import FileResponse
    from fastapi.concurrency import run_in_threadpool
    from pydantic import BaseModel

    router = APIRouter(tags=["XBRL"])

    class XbrlExtractRequest(BaseModel):
        ticker: str
        form: str = "10-K"

    @router.post("/api/extract-from-xbrl")
    async def extract_from_xbrl(payload: XbrlExtractRequest):
        """XBRL comparison route: fetch the latest US filing's XBRL facts and
        map them straight into the options schema + Excel (no PDF, no LLM).
        Synchronous — typically finishes in well under a minute."""
        ticker = (payload.ticker or "").strip().upper()
        if not ticker:
            raise HTTPException(status_code=400, detail="ticker is required")
        form = (payload.form or "10-K").strip().upper()
        try:
            result = await run_in_threadpool(run_xbrl_extraction, ticker, form)
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"XBRL extraction failed: {exc}")
        return {
            "status": "completed",
            "source": "xbrl",
            **result["meta"],
            "coverage": result["coverage"],
            "extraction": result["extraction"],
            "excel_download": f"/api/xbrl/download/{result['excel_file']}",
            "json_download": f"/api/xbrl/download/{result['json_file']}",
        }

    @router.get("/api/xbrl/excel/options")
    async def xbrl_excel_options(ticker: str = "", form: Optional[str] = None,
                                 mode: str = "rules"):
        """XBRL twin of GET /api/excel/options — SAME response format:
            {"ticker", "currency", "option_plans": [{count_mn, strike,
             maturity_years, kind}], "error"? }
        but sourced from SEC XBRL facts (no PDF, no LLM), and STRICT: fields
        hold the exact disclosed value or null — no 4.0 maturity default, no
        0.1 strike floor. Mirrors the US dual-form behavior: when no form is
        pinned, the latest 10-Q is tried first and the 10-K is the fallback.
        NEVER 500s — on any failure it returns option_plans: [] plus an
        "error" field."""

        import time as _time

        # Accept exchange-prefixed tickers ("NASDAQ:AAPL" -> AAPL).
        ticker = (ticker or "").strip().upper().split(":")[-1].strip()

        def _log(msg: str) -> None:
            print(f"[xbrl-excel {ticker or '?'}] {msg}", flush=True)

        if not ticker:
            _log("REJECTED: ticker is required")
            return {"ticker": ticker, "currency": None, "option_plans": [],
                    "error": "ticker is required"}

        mode = (mode or "rules").strip().lower()
        if mode not in ("rules", "ai"):
            return {"ticker": ticker, "currency": None, "option_plans": [],
                    "error": f"unknown mode {mode!r} (use 'rules' or 'ai')"}

        def _run(form_value: str):
            """One XBRL run in the chosen mode.
            Returns (plans|None, currency|None, error|None)."""
            t0 = _time.perf_counter()
            try:
                xbrl, meta = _fetch_xbrl(ticker, form_value)
                _log(f"{form_value}: fetched {meta.get('accession')} "
                     f"filed {meta.get('filing_date')} "
                     f"({_time.perf_counter() - t0:.1f}s)")
                if mode == "ai":
                    plans, ccy = _ai_assemble_plans(xbrl, log=_log)
                    ccy = ccy or "USD"
                else:
                    extraction = extract_options_from_xbrl(xbrl, log=_log)
                    plans = _map_plans_to_excel_strict(extraction, log=_log)
                    ccy = extraction.get("currency")
                _log(f"{form_value}: {mode} run done — {len(plans)} plan(s), "
                     f"total {_time.perf_counter() - t0:.1f}s")
                return plans, ccy, None
            except Exception as exc:
                _log(f"{form_value}: FAILED after "
                     f"{_time.perf_counter() - t0:.1f}s — "
                     f"{type(exc).__name__}: {exc}")
                return None, None, f"{type(exc).__name__}: {exc}"

        explicit_form = bool((form or "").strip())
        forms = [form.strip().upper()] if explicit_form else ["10-Q", "10-K"]
        _log(f"request: form={form or '(none)'} mode={mode} -> will try {forms}")

        t_req = _time.perf_counter()
        last_err = None
        currency = None
        for f in forms:
            plans, ccy, err = await run_in_threadpool(_run, f)
            if plans is not None:
                currency = ccy or currency
                if plans:
                    _log(f"RESPONSE from {f} ({mode}): {len(plans)} plan(s), "
                         f"{_time.perf_counter() - t_req:.1f}s total")
                    out = {"ticker": ticker, "currency": currency,
                           "option_plans": plans}
                    if mode == "ai":
                        out["mode"] = "ai"
                    return out
                _log(f"{f}: {mode} run produced no usable plans"
                     + (" — falling back to next form"
                        if f != forms[-1] else ""))
            else:
                last_err = err

        # ── IR-website fallback (user rule 2026-07-09): neither the 10-Q
        # nor the 10-K produced any options data -> pull the annual report
        # from the company's own IR site and run the PDF workflow on it. ──
        _log("no options data from 10-Q/10-K — trying IR website annual report")
        try:
            plans, ccy = await run_in_threadpool(_ir_fallback_plans, ticker, _log)
            if plans:
                _log(f"RESPONSE from IR annual report ({mode}): "
                     f"{len(plans)} plan(s), "
                     f"{_time.perf_counter() - t_req:.1f}s total")
                out = {"ticker": ticker, "currency": ccy or currency,
                       "option_plans": plans, "source": "ir_annual_report"}
                if mode == "ai":
                    out["mode"] = "ai"
                return out
            _log("IR fallback produced no usable plans")
        except Exception as exc:
            _log(f"IR fallback FAILED — {type(exc).__name__}: {exc}")
            ir_err = f"IR fallback: {type(exc).__name__}: {exc}"
            last_err = f"{last_err}; {ir_err}" if last_err else ir_err

        out = {"ticker": ticker, "currency": currency, "option_plans": []}
        if mode == "ai":
            out["mode"] = "ai"
        if last_err:
            out["error"] = last_err
        _log(f"RESPONSE: empty option_plans "
             f"(error={last_err or 'none'}), "
             f"{_time.perf_counter() - t_req:.1f}s total")
        return out

    @router.get("/api/xbrl/download/{filename}")
    async def download_xbrl_output(filename: str):
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(status_code=400, detail="invalid filename")
        path = OUTPUT_DIR / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        media = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if path.suffix == ".xlsx" else "application/json"
        )
        return FileResponse(path=path, filename=path.name, media_type=media)

except ImportError:  # CLI use without fastapi installed
    router = None


# ═════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    import json as _json

    # json_to_excel prints "✓" marks; Windows consoles default to cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Extract options data from SEC XBRL")
    ap.add_argument("ticker", help="US ticker, e.g. MSFT")
    ap.add_argument("--form", default="10-K", help="Form type (default 10-K)")
    args = ap.parse_args()

    res = run_xbrl_extraction(args.ticker, args.form)
    print(f"\nCompany : {res['extraction'].get('company_name')}")
    print(f"Period  : {res['extraction'].get('report_period')}")
    print(f"Filing  : {res['meta'].get('accession')} ({res['meta'].get('filing_date')})")
    print(f"Plans   : {res['coverage']['plan_count']}")
    for c in res["coverage"]["plans"]:
        print(f"\n  {c['plan_name']}")
        print(f"    filled : {', '.join(c['fields_filled']) or '(none)'}")
        print(f"    prior year: {'yes' if c['has_prior_year'] else 'no'}")
    print(f"\nJSON  -> {OUTPUT_DIR / res['json_file']}")
    print(f"Excel -> {OUTPUT_DIR / res['excel_file']}")
    print("\nFull extraction JSON:")
    print(_json.dumps(res["extraction"], indent=2, default=str))
