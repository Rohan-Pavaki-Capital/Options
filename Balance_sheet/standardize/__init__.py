"""Stage 3 — standardization package (the LLM mapper and its prompts).

  standardizer.py — extracts the line-item checklist in code, builds the
                    prompt, calls the LLM, validates/repairs the JSON.
  prompts/        — the region-specific system prompts (US/EU/AU).

`standardizer` is exposed as a submodule so callers use it as before, e.g.
`from .standardize import standardizer` then `standardizer.standardize(...)`.
"""

from . import standardizer

__all__ = ["standardizer"]
