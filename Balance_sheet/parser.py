"""Stage 2 — parse the captured balance-sheet page(s) to markdown via LlamaParse.

Only the small temp PDF from Stage 1 is sent (not the whole filing) to keep
the parse cheap and clean. LlamaParse is used in its table-friendly markdown
mode; the raw markdown is returned for the LLM.
"""

import asyncio
import concurrent.futures
import logging

from .config import require_llamaparse_key

logger = logging.getLogger("balance_sheet.parser")


def _import_llamaparse():
    """Import LlamaParse lazily so the rest of the package (Stage 1, tally)
    works even if the llama-parse distribution is missing."""
    try:  # package was renamed; support both distributions
        from llama_parse import LlamaParse
    except ImportError:
        try:
            from llama_cloud_services import LlamaParse
        except ImportError as exc:
            raise RuntimeError(
                "llama-parse is not installed - run "
                "'pip install -r Balance_sheet/requirements.txt'."
            ) from exc
    return LlamaParse


def _run_async(coro):
    """Run a coroutine to completion from ANY context. LlamaParse's sync
    load_data() cannot detect an async backend when called in a plain worker
    thread (e.g. via FastAPI's run_in_threadpool) and silently returns no
    documents — so we drive its async API with an explicit event loop:
    asyncio.run() when no loop is running here, a fresh loop in a helper
    thread when one is."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # plain thread/script — own the loop
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def parse_to_markdown(pdf_path: str) -> str:
    """Parse the (1-2 page) balance-sheet PDF to markdown. Raises on failure."""
    api_key = require_llamaparse_key()
    LlamaParse = _import_llamaparse()
    parser = LlamaParse(api_key=api_key, result_type="markdown")

    documents = _run_async(parser.aload_data(pdf_path))
    markdown = "\n\n".join(d.text for d in documents if getattr(d, "text", None))

    if not markdown.strip():
        raise RuntimeError("LlamaParse returned empty markdown for the balance-sheet pages.")

    logger.info("LlamaParse returned %d characters of markdown.", len(markdown))
    return markdown
