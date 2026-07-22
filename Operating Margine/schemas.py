"""Pydantic input/output models for the OperatingMargine service."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class HistoricalPeriod(BaseModel):
    period: str
    revenue: float
    op_income: float
    op_margin: float


class ForwardEstimate(BaseModel):
    period: str
    revenue_est: Optional[float] = None
    eps_est: Optional[float] = None


class MarginRequest(BaseModel):
    ticker: str
    company_name: str
    country: str
    industry_us: str
    industry_global: str
    currency: str
    units: str
    historicals: list[HistoricalPeriod] = Field(
        ..., description="TTM + up to 10 years, most recent first"
    )
    rd_adjusted_margins: Optional[list[float]] = None
    revenue_cagr_5y: Optional[float] = None
    revenue_cagr_10y: Optional[float] = None
    forward_estimates: list[ForwardEstimate] = Field(
        ...,
        description=(
            "4 rows: current FY + next 3 FYs, from the workbook's Simply Wall St "
            "consensus block (Input sheet L34:L37 revenue, M34:M37 EPS)"
        ),
    )
    nol_carryforward: Optional[float] = None
    past_avg_margin: Optional[float] = None
    damodaran_us_margin: float = Field(..., description="Workbook Input sheet J26")
    damodaran_global_margin: float = Field(..., description="Workbook Input sheet K26")
    mature_state_anchor: Optional[float] = Field(
        None,
        description="Fallback anchor when the Damodaran anchor is distorted by loss-makers",
    )
    mature_state_anchor_source: Optional[str] = None
    industry_revenue_growth_us: Optional[float] = None
    industry_revenue_growth_global: Optional[float] = None
    context_url: Optional[str] = Field(
        None, description="Optional IR/wiki URL scraped via Firecrawl for context"
    )


Classification = Literal[
    "SLOW_GROWER", "STALWART", "FAST_GROWER", "CYCLICAL", "TURNAROUND", "ASSET_PLAY"
]
Confidence = Literal["high", "medium", "low"]


class MarginResponse(BaseModel):
    status: Literal["ok", "error"]
    ticker: str
    classification: Optional[Classification] = None
    target_margin: Optional[float] = None
    convergence_year: Optional[int] = None
    confidence: Optional[Confidence] = None
    margin_driver: Optional[bool] = Field(
        None,
        description=(
            "true = the margin judgment is the value driver; "
            "false = ASSET_PLAY, margin logic is not the driver"
        ),
    )
    anchor_bypassed: Optional[bool] = Field(
        None, description="true when the distorted-anchor rule was applied"
    )
    damodaran_anchor_used: Optional[float] = None
    comps_used: Optional[list[str]] = None
    rationale: Optional[str] = None
    model: Optional[str] = None
    timestamp: Optional[str] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
