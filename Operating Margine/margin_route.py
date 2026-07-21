"""margin_route.py — Operating Margin as a section of the main backend.

Exposes an APIRouter that backend.py mounts on the SAME origin / uvicorn
server as the options pipeline, Balance Sheet, Simply Wall St, etc.:

  POST /api/operating-margin  ->  LLM-judged target pre-tax operating margin
                                  (EBIT % of sales, year 10) + convergence year

The "Operating Margine" folder name contains a space, so backend.py loads
this file via importlib from its path. The sibling modules (config, schemas,
service, ...) use plain top-level imports, so this folder is appended to
sys.path first — appended, not prepended, so it can never shadow root modules.
"""

import sys
from pathlib import Path

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.append(_DIR)

from fastapi import APIRouter  # noqa: E402

from schemas import MarginRequest, MarginResponse  # noqa: E402
from service import judge_operating_margin  # noqa: E402

router = APIRouter(tags=["Operating Margin"])


@router.post(
    "/api/operating-margin",
    response_model=MarginResponse,
    response_model_exclude_none=True,
    summary="Target pre-tax operating margin (year 10) + convergence year",
)
async def operating_margin(request: MarginRequest) -> MarginResponse:
    return await judge_operating_margin(request)
