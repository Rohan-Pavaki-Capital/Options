"""Balance_sheet — standardize a 10-Q/10-K balance sheet into a fixed
Damodaran-style template, reconciled against the filing's printed totals.

Pipeline: locate pages (PyMuPDF) -> parse to markdown (LlamaParse)
          -> standardize (Together AI LLM) -> tally check (code).
"""

from .pipeline import run_pipeline

__all__ = ["run_pipeline"]
