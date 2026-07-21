"""Stage 3 — standardize the balance-sheet markdown into the fixed schema.

Orchestrates the LLM call: builds the system + user messages, calls the chat
LLM (Together AI, OpenAI-compatible), and parses/validates the JSON,
re-prompting once on failure. The heavy lifting lives in sibling modules:
  line_items.py — code-side extraction of the line-item checklist
  validation.py — JSON strip / repair / schema validation

`extract_line_items` is re-exported here so callers keep using it as
`standardizer.extract_line_items(...)`.

NOTE: LlamaParse only parses PDFs to markdown; the schema-mapping reasoning
happens here, in a chat LLM.
"""

import json
import logging

from openai import OpenAI

from ..config import LLM_BASE_URL, LLM_MODEL, empty_result, require_together_key
from .prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_EU, SYSTEM_PROMPT_AU
from ..France import SYSTEM_PROMPT_FR
from .line_items import extract_line_items
from .validation import parse_and_validate

logger = logging.getLogger("balance_sheet.standardizer")


def _build_user_message(markdown: str, page_text: str | None = None) -> str:
    schema = json.dumps(empty_result(), indent=2)
    items = extract_line_items(markdown, page_text=page_text)
    items_block = ""
    if len(items) >= 5:
        listing = "\n".join(f"- {label}: {value}" for label, value in items)
        items_block = (
            "LINE ITEMS (extracted in code from the most-recent column; "
            "subtotal/total rows already removed). Bucket EVERY asset and "
            "liability line below into exactly one bucket or memo field "
            "using these EXACT values — do not re-read them from the "
            "markdown. The [ASSET]/[LIABILITY]/[EQUITY] tag is the filing "
            "section the line sits in: [ASSET] lines go ONLY into asset "
            "buckets/memo fields, [LIABILITY] lines ONLY into liability "
            "buckets/memo (never into an asset bucket like tax), and "
            "[EQUITY] lines stay out of the buckets entirely. Each line is "
            "also tagged (current) or (non-current) — map it into the "
            "matching current vs non-current bucket, and treat two "
            "identically-named lines with different tags as SEPARATE lines "
            "counted once each. A line with neither tag: use your own "
            "current-vs-non-current judgement rule:\n"
            f"{listing}\n\n"
        )
    return (
        "TARGET SCHEMA (return EXACTLY this shape; the bucket keys are FIXED — "
        "do not invent new keys):\n"
        f"{schema}\n\n"
        "NUMBERS: no digit-group commas inside a number. When several "
        "balance-sheet lines map into the same bucket (or memo field), do "
        "NOT add them yourself — write the printed values joined by ' + ' "
        'as a JSON STRING (e.g. "other_assets": "100 + 200 + 300") and the '
        "caller will compute the exact sum. Never any other arithmetic. "
        "Every number you write must "
        "be copied character-for-character from the most-recent column of "
        "the markdown — never write a number that does not appear there, and "
        "never repeat a line's value in a second bucket.\n\n"
        f"{items_block}"
        "MAPPING HINTS for lines with no named bucket (they must NEVER be "
        "dropped, or the totals will not reconcile):\n"
        "- Cash and cash equivalents + current marketable/short-term "
        "securities -> memo_excluded.cash_and_marketable_securities (NOT "
        "other_current_assets). Restricted cash, prepaid expenses, current "
        "derivative/receivable odds and ends -> current.other_current_assets.\n"
        "- Goodwill -> memo_excluded.goodwill ; intangible assets (net, "
        "excluding goodwill) -> memo_excluded.intangibles (NEVER into "
        "other_assets). Non-current derivatives, deferred tax assets -> "
        "non_current.other_assets.\n"
        "- Right-of-use assets -> lease_assets (non_current unless the "
        "filing shows a current portion).\n"
        "- Interest-bearing debt splits by the filing's classification: "
        "current portion of long-term debt / notes payable / short-term "
        "borrowings -> current.debt ; long-term debt / non-current "
        "borrowings -> memo_excluded.long_term_debt (NEVER into "
        "current.debt or other_liabilities).\n"
        "- Reconciliation to hit: sum(asset buckets) + memo cash + goodwill "
        "+ intangibles == printed total assets ; sum(liability buckets) + "
        "memo long_term_debt == printed total liabilities (within filing "
        "rounding).\n"
        "- Real estate blocks (Land / Buildings and improvements / less "
        "Accumulated depreciation) belong TOGETHER in exactly ONE bucket: "
        "real_estate_assets for REITs/property companies (include the "
        "negative accumulated depreciation), ppe otherwise. Never repeat or "
        "split the same property block across ppe AND real_estate_assets.\n"
        "- NEVER map subtotal or total rows (current-assets subtotal, total "
        "assets, total liabilities, total equity) into any bucket — "
        "individual line items only, or they will be double-counted.\n\n"
        "BALANCE SHEET MARKDOWN:\n"
        f"{markdown}"
    )


def _call_llm(messages: list[dict]) -> str:
    client = OpenAI(base_url=LLM_BASE_URL, api_key=require_together_key())
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0,
        max_tokens=4096,
        # JSON output mode + the provider's disable-reasoning switch:
        # thinking-by-default models (GLM, DeepSeek, Qwen3) otherwise burn the
        # budget on reasoning tokens and leave .content empty.
        response_format={"type": "json_object"},
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    message = response.choices[0].message
    content = message.content or ""
    if not content:
        # Some reasoning models still return the reply in the reasoning field.
        content = (getattr(message, "reasoning_content", None)
                   or getattr(message, "reasoning", None) or "")
    return content


def _system_prompt(region: str | None) -> str:
    """Pick the Stage-3 system prompt by company origin: "au" -> the
    Australian (AASB/IFRS) prompt (prompts/prompt_au.py); "fr" -> the French
    prompt (France/prompt_fr.py — EU logic plus the 'Total passif' trap
    rules); "eu" -> the European/IFRS prompt (prompts/prompt_eu.py); anything
    else -> the default US-oriented prompt (prompts/prompt_us.py)."""
    if region == "au":
        return SYSTEM_PROMPT_AU
    if region == "fr":
        return SYSTEM_PROMPT_FR
    return SYSTEM_PROMPT_EU if region == "eu" else SYSTEM_PROMPT


def standardize(markdown: str, region: str | None = None,
                page_text: str | None = None) -> dict:
    """Map the balance-sheet markdown into the fixed schema. Re-prompts once
    if the LLM output fails to parse/validate; raises if it fails twice.
    `page_text` = Stage-1 page text, used to validate/realign the checklist's
    label-value pairing (see line_items._realign_with_page_text)."""
    messages = [
        {"role": "system", "content": _system_prompt(region)},
        {"role": "user", "content": _build_user_message(markdown, page_text)},
    ]
    raw = _call_llm(messages)
    try:
        return parse_and_validate(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("LLM output invalid (%s) — re-prompting once.", exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Your previous output was invalid: {exc}. Return ONLY a "
                    "valid JSON object matching the target schema exactly — "
                    "all fixed keys present, no extra keys, no markdown "
                    "fences, no prose."
                ),
            },
        ]
        raw = _call_llm(retry_messages)
        return parse_and_validate(raw)  # let a second failure raise


def restandardize(markdown: str, previous_json: dict, gap_message: str,
                  region: str | None = None,
                  page_text: str | None = None) -> dict:
    """Tally-failure hook (Stage 4): re-call the LLM with the previous JSON
    and the exact gap so it can re-map missing/double-counted lines. Called
    in a loop by the pipeline until balanced or max retries."""
    messages = [
        {"role": "system", "content": _system_prompt(region)},
        {"role": "user", "content": _build_user_message(markdown, page_text)},
        {"role": "assistant", "content": json.dumps(previous_json)},
        {
            "role": "user",
            "content": (
                f"CORRECTION note: {gap_message} Correct the previous JSON with the SMALLEST "
                "possible edit: change ONLY the bucket(s) implicated by the "
                "gap(s) above and copy every other bucket value UNCHANGED "
                "from the previous JSON. When a bucket holds several lines, "
                "rewrite it as the printed values joined by ' + ' as a JSON "
                'STRING (e.g. "100 + 200"), copying each number character-'
                "for-character from the "
                "most-recent column of the markdown — never a value already "
                "placed in another bucket, never a subtotal row, never a "
                "number that is not printed there. Numbers exactly as printed "
                "(strip only '$' and commas), same fixed schema — "
                "preferred_stock and mezzanine_equity are TOP-LEVEL keys of "
                '"liabilities", never inside non_current or current. Return '
                "ONLY the corrected JSON object."
            ),
        },
    ]
    raw = _call_llm(messages)
    try:
        return parse_and_validate(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        # Same one-shot schema repair standardize() gets: a correction that
        # dies on shape (e.g. preferred_stock nested under non_current) would
        # otherwise abort the whole tally loop.
        logger.warning("Correction output invalid (%s) — re-prompting once.",
                       exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Your previous output was invalid: {exc}. Return ONLY a "
                    "valid JSON object matching the target schema exactly — "
                    "all fixed keys present, no extra keys (preferred_stock "
                    "and mezzanine_equity are TOP-LEVEL keys of "
                    '"liabilities", never inside non_current or current), '
                    "no markdown fences, no prose."
                ),
            },
        ]
        raw = _call_llm(retry_messages)
        return parse_and_validate(raw)  # let a second failure raise
