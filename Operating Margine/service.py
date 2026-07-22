"""Core judgment flow, shared by the standalone app (main.py) and the
backend-mounted route (margin_route.py)."""

import logging
import time
from datetime import datetime, timezone

import config
from llm import call_llm
from prompts import SYSTEM_PROMPT, build_retry_suffix, build_user_prompt
from schemas import MarginRequest, MarginResponse
from scraper import fetch_company_context
from validator import JudgmentValidationError, validate_judgment

logger = logging.getLogger("operating_margine")


def _error_response(ticker: str, code: str, detail: str) -> MarginResponse:
    return MarginResponse(
        status="error",
        ticker=ticker,
        error_code=code,
        error_detail=detail,
        model=config.MODEL_NAME,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


async def judge_operating_margin(request: MarginRequest) -> MarginResponse:
    """Run the full flow: optional context scrape -> LLM -> validate (retry once)."""
    started = time.perf_counter()

    qualitative_context = None
    if request.context_url:
        qualitative_context = await fetch_company_context(request.context_url)

    user_prompt = build_user_prompt(request, qualitative_context)

    judgment = None
    last_error = ""
    for attempt in range(2):  # initial call + one validation retry
        prompt = user_prompt if attempt == 0 else user_prompt + build_retry_suffix(last_error)
        try:
            raw = await call_llm(SYSTEM_PROMPT, prompt)
        except Exception as exc:
            logger.error("ticker=%s llm_error=%s", request.ticker, exc)
            return _error_response(request.ticker, "LLM_CALL_FAILED", str(exc))
        try:
            judgment = validate_judgment(raw, request)
            break
        except JudgmentValidationError as exc:
            last_error = str(exc)
            logger.warning(
                "ticker=%s validation attempt %d failed: %s",
                request.ticker,
                attempt + 1,
                last_error,
            )

    latency_ms = (time.perf_counter() - started) * 1000

    if judgment is None:
        logger.error(
            "ticker=%s status=error latency_ms=%.0f detail=%s",
            request.ticker,
            latency_ms,
            last_error,
        )
        return _error_response(request.ticker, "llm_rule_violation", last_error)

    logger.info(
        "ticker=%s classification=%s target_margin=%.4f latency_ms=%.0f",
        request.ticker,
        judgment["classification"],
        judgment["target_margin"],
        latency_ms,
    )

    return MarginResponse(
        status="ok",
        ticker=request.ticker,
        classification=judgment["classification"],
        target_margin=judgment["target_margin"],
        convergence_year=judgment["convergence_year"],
        confidence=judgment["confidence"],
        margin_driver=judgment["margin_driver"],
        anchor_bypassed=judgment["anchor_bypassed"],
        damodaran_anchor_used=float(judgment["damodaran_anchor_used"]),
        comps_used=judgment["comps_used"],
        rationale=judgment["rationale"],
        model=config.MODEL_NAME,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
