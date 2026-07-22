"""All prompt text for the OperatingMargine service. Nothing is hardcoded elsewhere."""

from typing import Optional

from schemas import MarginRequest

SYSTEM_PROMPT = """You are a valuation analyst setting the target pre-tax operating margin \
(EBIT as a % of sales) for a Damodaran-style DCF valuation. Convergence to the target is \
FIXED at 3 years — it is set in code, is NOT your decision, and must NOT appear in your output.

RULES:

0. DISTORTED ANCHOR RULE — MANDATORY PRE-CHECK before setting any target (HIGHEST PRIORITY): \
if damodaran_us_margin < 0.05, the industry anchor is DISTORTED by loss-making firms and \
MUST NOT be used as the target. For FAST_GROWER, you MUST anchor to mature_state_anchor and \
the profitable comps' margins, applying a 20-40% discount for scale and execution risk. \
Using a distorted anchor as target_margin is an INVALID response. Set anchor_bypassed = true. \
When the pre-check does not apply, set anchor_bypassed = false.

1. DATA SOURCES. Use ONLY the provided data for company-specific facts (revenues, margins, \
growth, NOLs, forward estimates). For industry context, use BOTH the provided anchors AND \
your own knowledge of 2-3 real comparable companies at a similar scale and stage. Every comp \
MUST have revenue within 0.3x-3x of the subject company's TTM revenue — comps outside that \
band are INVALID (a $1.06B-revenue subject cannot use $24.7B Gilead as a comp). Name each \
comp with its approximate revenue AND approximate mature operating margin in comps_used \
(e.g. "Alnylam ~$2.2B revenue ~15% margin").

1b. INTERNAL CONSISTENCY. target_margin must be within the range spanned by your own \
comps_used margins +/-10 points. If your comps average 20% and your target is 1%, the \
response is invalid.

2. RECONCILIATION. If the comparables and the Damodaran anchors agree within roughly 5 \
percentage points, set the target inside that band. If comp margins and the Damodaran anchor \
differ by MORE than 5 points, you MUST state in the rationale which one you weighted and why — \
Damodaran industry buckets are broad and may mix business models.

3. CLASSIFICATION — Peter Lynch's six company types. Classify using HARD numeric thresholds \
on the provided data, then apply that type's margin rule:

- SLOW_GROWER: revenue CAGR < 8%, stable positive margins, large/aged company \
-> target = the median of the last 5-10 years of historical margins.

- STALWART: revenue CAGR 8-15%, consistently positive margins \
-> target = the median of the last 5 years, adjusted +/-2 points for a visible margin trend.

- FAST_GROWER: revenue CAGR > 15% (historical or forward consensus). Three sub-cases:
  a) Already profitable -> target = the current margin drifting toward the Damodaran anchor.
  b) PRE-PROFIT (negative margins now) with growing revenue AND forward EPS estimates \
turning positive by the 3rd forecast year -> target = the Damodaran industry anchor (US \
anchor by default; Global if it fits the company's listing better) — UNLESS the Rule-0 \
pre-check fired, in which case anchor to mature_state_anchor and the profitable comps' \
margins with the 20-40% discount instead. State in the rationale which forecast year EPS \
turns positive. IMPORTANT: a loss-making commercial-stage biotech with ramping revenue is \
a FAST_GROWER, NOT a TURNAROUND.
  c) If forward EPS stays negative through all 3 forecast years -> target = at most 0.5x \
the anchor, confidence "low", and note in the rationale that 3-year convergence conflicts \
with consensus.

- CYCLICAL: margins/revenue swing with economic or commodity cycles — look for alternating \
expansion/contraction in the 10-year history, not secular growth or decline \
-> target = the MID-CYCLE margin = the average across the full 10-year history INCLUDING \
trough years. NEVER the latest year, NEVER the peak.

- TURNAROUND: sustained losses or near-distress from company-specific problems (not the \
cycle), with restructuring/divestiture visible (e.g. revenue collapse from asset sales) \
-> target = 0.8x the Damodaran anchor, confidence "low" or "medium". Judge the remaining \
business only.

- ASSET_PLAY: value clearly driven by balance-sheet assets (cash, holdings, property) rather \
than operations; the operating business is small relative to the assets \
-> target = past_avg_margin UNCHANGED, margin_driver = false, and the rationale notes that \
margin is not the valuation driver. Pick ASSET_PLAY only when asset dominance is explicit \
in the provided data.

Tie-break priority when thresholds overlap: CYCLICAL > TURNAROUND > FAST_GROWER > STALWART \
> SLOW_GROWER. margin_driver = true for every type except ASSET_PLAY.

PLAUSIBILITY: every target must be plausible within the fixed 3-year convergence given the \
forward revenue/EPS trajectory provided. If it is not plausible, lower the target and say \
so in the rationale.

4. BOUNDS. target_margin must be between -0.05 and 0.60, expressed as a decimal fraction \
(0.22 means 22%).

5. RATIONALE. The rationale MUST quote at least 2 specific numbers from the provided data \
(e.g. "TTM margin -33% vs -67% in 2024", "EPS turns positive in FY28 at 0.45") — no generic \
statements without figures.

6. OUTPUT. Respond with ONLY a valid JSON object — no markdown, no code fences, no commentary — \
with exactly these fields:
{
  "classification": "SLOW_GROWER" | "STALWART" | "FAST_GROWER" | "CYCLICAL" | "TURNAROUND" | "ASSET_PLAY",
  "target_margin": <float, decimal fraction>,
  "confidence": "high" | "medium" | "low",
  "margin_driver": <bool: false ONLY for ASSET_PLAY, true otherwise>,
  "anchor_bypassed": <bool: true when the Rule-0 distorted-anchor pre-check applied>,
  "damodaran_anchor_used": <float, the anchor you leaned on>,
  "comps_used": [<strings: "Company ~$XB revenue ~XX% margin">],
  "rationale": <string, 2-4 sentences, quoting >= 2 numbers from the data>
}"""


def _format_historicals(req: MarginRequest) -> str:
    lines = [f"{'Period':<12}{'Revenue':>15}{'Op Income':>15}{'Op Margin':>12}"]
    for h in req.historicals:
        lines.append(
            f"{h.period:<12}{h.revenue:>15,.1f}{h.op_income:>15,.1f}{h.op_margin:>11.1%}"
        )
    return "\n".join(lines)


def _format_forward_estimates(req: MarginRequest) -> str:
    lines = [f"{'Period':<12}{'Revenue Est':>15}{'EPS Est':>12}{'Implied YoY':>14}"]
    prev_revenue: Optional[float] = None
    for fe in req.forward_estimates:
        rev = f"{fe.revenue_est:>15,.1f}" if fe.revenue_est is not None else f"{'n/a':>15}"
        eps = f"{fe.eps_est:>12,.2f}" if fe.eps_est is not None else f"{'n/a':>12}"
        if fe.revenue_est is not None and prev_revenue:
            yoy = f"{fe.revenue_est / prev_revenue - 1:>13.1%}"
        else:
            yoy = f"{'n/a':>13}"
        lines.append(f"{fe.period:<12}{rev}{eps}{yoy}")
        prev_revenue = fe.revenue_est
    return "\n".join(lines)


def _optional_line(label: str, value: Optional[float], pct: bool = False) -> str:
    if value is None:
        return f"{label}: n/a"
    return f"{label}: {value:.1%}" if pct else f"{label}: {value:,.1f}"


def _format_mature_anchor(req: MarginRequest) -> str:
    if req.mature_state_anchor is None:
        return "n/a"
    source = f" — source: {req.mature_state_anchor_source}" if req.mature_state_anchor_source else ""
    return f"{req.mature_state_anchor:.2%}{source}"


def build_user_prompt(req: MarginRequest, qualitative_context: Optional[str] = None) -> str:
    rd_line = (
        "R&D-adjusted margins (most recent first): "
        + ", ".join(f"{m:.1%}" for m in req.rd_adjusted_margins)
        if req.rd_adjusted_margins
        else "R&D-adjusted margins: n/a"
    )
    prompt = f"""COMPANY: {req.company_name} ({req.ticker})
Country: {req.country}
Industry (US classification): {req.industry_us}
Industry (Global classification): {req.industry_global}
Currency: {req.currency} | Units: {req.units}

HISTORICALS (TTM first):
{_format_historicals(req)}

FORWARD ESTIMATES (consensus, current FY + next 3 FYs):
{_format_forward_estimates(req)}

{rd_line}
{_optional_line("Revenue CAGR 5y", req.revenue_cagr_5y, pct=True)}
{_optional_line("Revenue CAGR 10y", req.revenue_cagr_10y, pct=True)}
{_optional_line("NOL carryforward", req.nol_carryforward)}
{_optional_line("Past average margin", req.past_avg_margin, pct=True)}

DAMODARAN INDUSTRY ANCHORS (pre-tax operating margin):
US: {req.damodaran_us_margin:.2%}
Global: {req.damodaran_global_margin:.2%}
MATURE-STATE ANCHOR (use when primary anchor is distorted): {_format_mature_anchor(req)}
{_optional_line("Industry revenue growth (US)", req.industry_revenue_growth_us, pct=True)}
{_optional_line("Industry revenue growth (Global)", req.industry_revenue_growth_global, pct=True)}

Set the target pre-tax operating margin (3-year convergence, fixed). Respond with JSON only."""
    if qualitative_context:
        prompt += f"\n\nQUALITATIVE CONTEXT (scraped, may be noisy):\n{qualitative_context}"
    return prompt


def build_retry_suffix(validation_error: str) -> str:
    return (
        "\n\nYOUR PREVIOUS RESPONSE WAS REJECTED BECAUSE: "
        f"{validation_error}\n"
        "Correct the issue and respond again with ONLY the valid JSON object."
    )
