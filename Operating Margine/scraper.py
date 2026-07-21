"""Firecrawl helper. Optional — NOT called in the default flow.

Fails silently: any error returns None and the caller proceeds without context.
"""

import logging
from typing import Optional

import httpx

import config

logger = logging.getLogger("operating_margine.scraper")

SCRAPE_TIMEOUT_SECONDS: float = 20.0


async def fetch_company_context(url: str) -> Optional[str]:
    """Scrape a company IR/wiki page via Firecrawl and return its markdown.

    Returns None on any failure (missing key, HTTP error, unexpected payload).
    """
    if not config.FIRECRAWL_API_KEY:
        logger.info("FIRECRAWL_API_KEY not set; skipping context scrape")
        return None
    try:
        async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                config.FIRECRAWL_SCRAPE_URL,
                headers={"Authorization": f"Bearer {config.FIRECRAWL_API_KEY}"},
                json={"url": url, "formats": ["markdown"]},
            )
            response.raise_for_status()
            payload = response.json()
        markdown = (payload.get("data") or {}).get("markdown")
        if not markdown:
            return None
        return markdown[: config.CONTEXT_MAX_CHARS]
    except Exception as exc:  # fail silently by design
        logger.warning("Firecrawl scrape failed for %s: %s", url, exc)
        return None
