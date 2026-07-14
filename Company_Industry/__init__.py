"""Company_Industry — GuruFocus industry -> Damodaran industry taxonomy.

Exposes ``router`` (a FastAPI APIRouter) mounted by app.py, serving
``GET /api/industry?ticker=AAPL``.
"""
from .industry_route import router

__all__ = ["router"]
