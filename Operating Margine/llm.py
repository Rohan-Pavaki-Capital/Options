"""Together AI client call with transport-level retry."""

import asyncio
import logging

from openai import APIError, AsyncOpenAI, RateLimitError

import config

logger = logging.getLogger("operating_margine.llm")

_client = AsyncOpenAI(api_key=config.TOGETHER_API_KEY, base_url=config.TOGETHER_BASE_URL)

TRANSPORT_RETRIES: int = 2
RETRY_BACKOFF_SECONDS: float = 2.0


async def call_llm(system_prompt: str, user_prompt: str) -> str:
    """Call Together AI and return the raw completion text.

    Retries transient transport/rate-limit errors; raises on final failure.
    """
    last_error: Exception | None = None
    for attempt in range(1 + TRANSPORT_RETRIES):
        try:
            response = await _client.chat.completions.create(
                model=config.MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=config.LLM_TEMPERATURE,
                max_tokens=config.LLM_MAX_TOKENS,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if not content:
                raise APIError("Empty completion content", request=None, body=None)
            return content
        except (RateLimitError, APIError) as exc:
            last_error = exc
            logger.warning("LLM call attempt %d failed: %s", attempt + 1, exc)
            if attempt < TRANSPORT_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError(f"LLM call failed after {1 + TRANSPORT_RETRIES} attempts: {last_error}")
