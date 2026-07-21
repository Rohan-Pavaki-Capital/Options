"""Preprocessing / parsing stages (before the LLM standardizer).

  pdf_locator.py — Stage 1: find the balance-sheet pages, export a temp PDF,
                   read the printed totals and unit label from the page text.
  parser.py      — Stage 2: send the temp PDF to LlamaParse, return markdown.

Both are exposed as submodules so callers use them exactly as before, e.g.
`from .preprocessing import parser, pdf_locator` then `pdf_locator.locate_balance_sheet(...)`.
"""

from . import parser, pdf_locator

__all__ = ["parser", "pdf_locator"]
