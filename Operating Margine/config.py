"""Environment configuration for the OperatingMargine service."""

import os

from dotenv import load_dotenv

load_dotenv()

TOGETHER_API_KEY: str = os.getenv("TOGETHER_API_KEY", "")
FIRECRAWL_API_KEY: str = os.getenv("FIRECRAWL_API_KEY", "")
MODEL_NAME: str = os.getenv("MODEL_NAME", "meta-llama/Llama-3.3-70B-Instruct-Turbo")

TOGETHER_BASE_URL: str = "https://api.together.xyz/v1"
FIRECRAWL_SCRAPE_URL: str = "https://api.firecrawl.dev/v1/scrape"

LLM_TEMPERATURE: float = 0.0
LLM_MAX_TOKENS: int = 500

# Max characters of scraped qualitative context appended to the user prompt.
CONTEXT_MAX_CHARS: int = 3000
