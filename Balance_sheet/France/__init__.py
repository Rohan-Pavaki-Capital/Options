"""France-specific balance-sheet handling (region="fr").

  prompt_fr.py — Stage-3 system prompt for French / IFRS filings. Same logic
                 as the European prompt (prompts/prompt_eu.py) plus the French
                 "Total passif" trap rules: French filers print the grand
                 total of the equity-and-liabilities side labeled just
                 "Total liabilities" (= total assets), e.g. Bolloré.
  guard.py     — deterministic CODE guard that re-derives
                 filing_totals.total_liabilities when the printed
                 "Total liabilities" equals the printed "Total assets".

standardizer._system_prompt() selects the prompt for region="fr"; the
pipeline applies the guard (after the printed-totals override) for
region="fr" only.
"""

from .prompt_fr import SYSTEM_PROMPT_FR
from .guard import fix_grand_total_liabilities

__all__ = ["SYSTEM_PROMPT_FR", "fix_grand_total_liabilities"]
