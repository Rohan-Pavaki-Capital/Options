"""Pydantic input/output models for the OperatingMargine service."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class HistoricalPeriod(BaseModel):
    period: str
    revenue: float
    op_income: float
    op_margin: float


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
    consensus_growth_5y: Optional[float] = None
    nol_carryforward: Optional[float] = None
    past_avg_margin: Optional[float] = None
    damodaran_us_margin: float = Field(..., description="Workbook Input sheet J26")
    damodaran_global_margin: float = Field(..., description="Workbook Input sheet K26")
    context_url: Optional[str] = Field(
        None, description="Optional IR/wiki URL scraped via Firecrawl for context"
    )


Classification = Literal["RAMPING", "MATURE", "DISTRESSED", "TRANSFORMING"]
Confidence = Literal["high", "medium", "low"]


class MarginResponse(BaseModel):
    status: Literal["ok", "error"]
    ticker: str
    classification: Optional[Classification] = None
    target_margin: Optional[float] = None
    convergence_year: Optional[int] = None
    confidence: Optional[Confidence] = None
    damodaran_anchor_used: Optional[float] = None
    comps_used: Optional[list[str]] = None
    rationale: Optional[str] = None
    model: Optional[str] = None
    timestamp: Optional[str] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
