"""Stage-3 standardization system prompts, one module per region.

  prompt_us.py — default (US GAAP + all other markets, incl. Japanese GAAP)
  prompt_eu.py — European / IFRS filings (region="eu")
  prompt_au.py — Australian / AASB filings (region="au")

The French prompt (region="fr") lives in Balance_sheet/France/prompt_fr.py,
next to its code guard.

standardizer._system_prompt() selects among these by the caller's region.
"""

from .prompt_us import SYSTEM_PROMPT
from .prompt_eu import SYSTEM_PROMPT_EU
from .prompt_au import SYSTEM_PROMPT_AU

__all__ = ["SYSTEM_PROMPT", "SYSTEM_PROMPT_EU", "SYSTEM_PROMPT_AU"]
