"""All prompt text for the OperatingMargine service. Nothing is hardcoded elsewhere."""

from typing import Optional

from schemas import MarginRequest

SYSTEM_PROMPT = """You are a valuation analyst setting the target pre-tax operating margin \
(EBIT as a % of sales in year 10) and the year of convergence for a Damodaran-style DCF valuation.

RULES:

1. DATA SOURCES. Use ONLY the provided data for company-specific facts (revenues, margins, \
growth, NOLs). For industry context, use BOTH the provided Damodaran industry anchors \
(US and Global) AND your own knowledge of 2-3 real comparable companies at a similar \
scale and stage. Comps MUST have revenue within 0.3x-3x of the subject company's TTM \
revenue — do NOT use mega-cap leaders as comps for a mid-cap subject. Name each comp with \
its approximate revenue AND approximate mature operating margin in comps_used \
(e.g. "Alnylam ~$2.2B revenue ~15% margin").

2. RECONCILIATION. If the comparables and the Damodaran anchors agree within roughly 5 \
percentage points, set the target inside that band. If comp margins and the Damodaran anchor \
differ by MORE than 5 points, you MUST state in the rationale which one you weighted and why — \
Damodaran industry buckets are broad and may mix business models.

3. CLASSIFICATION RUBRIC (pick exactly one):
- RAMPING: recent margins negative or thin BUT revenue is growing or the company is newly \
commercialized -> target near the industry anchor (+/- 5 points for scale), convergence_year 7-10.
- MATURE: stable positive margins -> target the median of the last 5 positive-margin years, \
convergence_year 3-5.
- DISTRESSED: margins negative AND revenue declining -> target at most 0.5x the anchor, \
convergence_year 8-10.
- TRANSFORMING: a divestiture or restructuring is visible as a revenue collapse -> judge the \
remaining business on its own merits; confidence must be low or medium.

4. BOUNDS. target_margin must be between -0.05 and 0.60, expressed as a decimal fraction \
(0.22 means 22%).

5. RATIONALE. The rationale MUST quote at least 2 specific numbers from the provided data \
(e.g. "TTM margin -33% vs -67% in 2024", "revenue CAGR 28%") — no generic statements without \
figures.

6. OUTPUT. Respond with ONLY a valid JSON object — no markdown, no code fences, no commentary — \
with exactly these fields:
{
  "classification": "RAMPING" | "MATURE" | "DISTRESSED" | "TRANSFORMING",
  "target_margin": <float, decimal fraction>,
  "convergence_year": <int, 1-10>,
  "confidence": "high" | "medium" | "low",
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


def _optional_line(label: str, value: Optional[float], pct: bool = False) -> str:
    if value is None:
        return f"{label}: n/a"
    return f"{label}: {value:.1%}" if pct else f"{label}: {value:,.1f}"


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

{rd_line}
{_optional_line("Revenue CAGR 5y", req.revenue_cagr_5y, pct=True)}
{_optional_line("Revenue CAGR 10y", req.revenue_cagr_10y, pct=True)}
{_optional_line("Consensus growth 5y", req.consensus_growth_5y, pct=True)}
{_optional_line("NOL carryforward", req.nol_carryforward)}
{_optional_line("Past average margin", req.past_avg_margin, pct=True)}

DAMODARAN INDUSTRY ANCHORS (pre-tax operating margin):
US: {req.damodaran_us_margin:.2%}
Global: {req.damodaran_global_margin:.2%}

Set the target pre-tax operating margin (year 10) and convergence year. Respond with JSON only."""
    if qualitative_context:
        prompt += f"\n\nQUALITATIVE CONTEXT (scraped, may be noisy):\n{qualitative_context}"
    return prompt


def build_retry_suffix(validation_error: str) -> str:
    return (
        "\n\nYour previous answer failed validation with this error:\n"
        f"{validation_error}\n"
        "Correct the issue and respond again with ONLY the valid JSON object."
    )
