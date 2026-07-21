"""OperatingMargine — standalone FastAPI app.

Run independently with:  uvicorn main:app --port 8000  (from this folder)
The same logic is also mounted into the main backend as POST /api/operating-margin
via margin_route.py — see backend.py at the project root.
"""

import logging

from fastapi import FastAPI

import config
from schemas import MarginRequest, MarginResponse
from service import judge_operating_margin

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)

app = FastAPI(title="OperatingMargine", version="1.0.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "model": config.MODEL_NAME}


@app.post("/operating-margin", response_model=MarginResponse, response_model_exclude_none=True)
async def operating_margin(request: MarginRequest) -> MarginResponse:
    return await judge_operating_margin(request)
