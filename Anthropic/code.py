"""
Stage 3 — Anthropic Claude Sonnet extraction logic.

Functions:
    extract_with_claude()       — two-pass extraction with retry and smart validation
    call_claude()               — single Messages API call with cost tracking
    parse_json_response()       — robust JSON parser handling fences, trailing commas, embedded JSON
    build_vision_content()      — interleave page images + text for vision-mode calls
    validate_rollforward()      — arithmetic check on a single plan (multiple formula support)
    validate_all_plans()        — arithmetic check across all plans
    validate_final_output()     — final quality validation with summary metrics
    merge_results()             — deep-merge per-batch extraction results
    normalize_currency()        — ISO 4217 currency code normalization

Verbosity:
    set_verbose(bool) — toggle stderr logging from options.py when --quiet is set
"""

import json
import re
import sys
import time

import cache
from Anthropic.prompt import SYSTEM_PROMPT, EXTRACTION_PROMPT, VALIDATION_PROMPT
from Anthropic.schema import OUTPUT_SCHEMA


VERBOSE = True


def set_verbose(value: bool):
    """Toggle stderr logging for this module."""
    global VERBOSE
    VERBOSE = value


def log(msg: str):
    if VERBOSE:
        print(msg, file=sys.stderr)


def _safe_format(template: str, **kwargs) -> str:
    """
    Template substitution that treats `{` and `}` as literal characters
    everywhere except the named placeholders we explicitly inject.

    Replaces `{key}` with the corresponding value for each key in kwargs.
    Does NOT use str.format(), so literal braces in the template (JSON
    examples, schema, page text) need no escaping.
    """
    result = template
    for k, v in kwargs.items():
        result = result.replace("{" + k + "}", str(v))
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# CURRENCY NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_currency(raw_currency: str) -> str:
    """
    Normalize various currency representations to ISO 4217 codes.

    Examples:
        '£' or 'GBP' or 'pounds' or 'pence' → 'GBP'
        '$' or 'USD' or 'dollars' or 'cents' → 'USD'
        'S$' or 'SGD' → 'SGD'
    """
    if not raw_currency:
        return None

    raw = raw_currency.strip().upper()

    mapping = {
        # Pound sterling
        "£": "GBP", "GBP": "GBP", "POUND": "GBP", "POUNDS": "GBP",
        "PENCE": "GBP", "PENNY": "GBP", "STERLING": "GBP",

        # US Dollar
        "$": "USD", "USD": "USD", "DOLLAR": "USD", "DOLLARS": "USD",
        "CENT": "USD", "CENTS": "USD", "US$": "USD",

        # Euro
        "€": "EUR", "EUR": "EUR", "EURO": "EUR", "EUROS": "EUR",

        # Singapore Dollar
        "S$": "SGD", "SGD": "SGD",

        # Hong Kong Dollar
        "HK$": "HKD", "HKD": "HKD",

        # Australian Dollar
        "A$": "AUD", "AUD": "AUD", "AU$": "AUD",

        # Canadian Dollar
        "C$": "CAD", "CAD": "CAD", "CA$": "CAD",

        # Japanese Yen
        "¥": "JPY", "JPY": "JPY", "YEN": "JPY",

        # Chinese Yuan / RMB
        "RMB": "CNY", "CNY": "CNY", "YUAN": "CNY", "RENMINBI": "CNY",

        # Korean Won
        "₩": "KRW", "KRW": "KRW", "WON": "KRW",

        # Indian Rupee
        "₹": "INR", "INR": "INR", "RUPEE": "INR", "RUPEES": "INR",

        # Swiss Franc
        "CHF": "CHF", "FRANC": "CHF", "FRANCS": "CHF",

        # Brazilian Real
        "R$": "BRL", "BRL": "BRL", "REAL": "BRL", "REAIS": "BRL",

        # Mexican Peso
        "MX$": "MXN", "MXN": "MXN",

        # South African Rand
        "ZAR": "ZAR", "RAND": "ZAR",

        # New Zealand Dollar
        "NZ$": "NZD", "NZD": "NZD",
    }

    return mapping.get(raw, raw_currency)  # Return original if no match


# ═══════════════════════════════════════════════════════════════════════════════
# CLAUDE MESSAGES API CALL
# ═══════════════════════════════════════════════════════════════════════════════

# Output-token budget for the two extraction passes. Large share-based-payment
# disclosures (many named plans + tranche/valuation tables) can produce JSON well
# past the old 8192 default, which truncated the response mid-object → invalid JSON
# (e.g. Renault FY2025). claude-sonnet-4-6 supports up to 64k output tokens.
EXTRACTION_MAX_TOKENS = 32768

# Above this output budget the Anthropic SDK requires streaming (a non-streaming
# request could exceed its 10-min timeout → ValueError before the call is sent).
# call_claude streams when max_tokens exceeds this; small calls stay non-streaming.
_STREAMING_THRESHOLD = 8192

def call_claude(client, system, user_content, model="claude-sonnet-4-6",
                max_tokens=8192, cost_tracker=None, use_prompt_cache=True):
    """
    Single call to Anthropic Messages API with cost tracking.

    When use_prompt_cache=True (and the global cache is enabled), the system
    prompt is sent as a cacheable content block with ephemeral cache_control.
    Anthropic caches it server-side for ~5 minutes, giving ~90% input-token
    cost reduction and faster latency on subsequent calls (Pass 2 validation,
    next batch, etc.) — provided the system prompt is ≥ 1024 tokens.
    """
    if use_prompt_cache and cache.is_enabled() and isinstance(system, str):
        system_arg = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]
    else:
        system_arg = system

    messages = [{"role": "user", "content": user_content}]

    # The SDK refuses a non-streaming request when max_tokens is high enough that
    # the call could exceed its 10-minute timeout (raises ValueError before the
    # request is sent). The large extraction passes hit that, so stream them and
    # accumulate the text; small classification calls stay on the simple path.
    if max_tokens > _STREAMING_THRESHOLD:
        text_parts = []
        with client.messages.stream(
            model=model, max_tokens=max_tokens, system=system_arg,
            messages=messages,
        ) as stream:
            for chunk in stream.text_stream:
                text_parts.append(chunk)
            final = stream.get_final_message()
        if cost_tracker is not None and getattr(final, "usage", None):
            cost_tracker.add_anthropic(
                getattr(final.usage, "input_tokens", 0) or 0,
                getattr(final.usage, "output_tokens", 0) or 0,
            )
        return "".join(text_parts).strip()

    resp = client.messages.create(
        model=model, max_tokens=max_tokens, system=system_arg,
        messages=messages,
    )
    if cost_tracker is not None and getattr(resp, "usage", None):
        cost_tracker.add_anthropic(
            getattr(resp.usage, "input_tokens", 0) or 0,
            getattr(resp.usage, "output_tokens", 0) or 0,
        )
    return resp.content[0].text.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# ROBUST JSON PARSING
# ═══════════════════════════════════════════════════════════════════════════════

def parse_json_response(raw: str) -> dict:
    """
    Robustly parse JSON from LLM response.
    Handles markdown fences, trailing commas, embedded JSON, and minor formatting issues.

    Returns None if parsing fails after all recovery attempts.
    """
    if not raw:
        return None

    cleaned = raw.strip()

    # Remove markdown code fences
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    # Try 1: Direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try 2: Fix trailing commas (common LLM issue)
    try:
        fixed = re.sub(r',(\s*[}\]])', r'\1', cleaned)
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # Try 3: Find JSON object embedded in text
    match = re.search(r'\{[\s\S]*\}', cleaned)
    if match:
        candidate = match.group()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Try with trailing comma fix
            try:
                fixed = re.sub(r',(\s*[}\]])', r'\1', candidate)
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass

    # Try 4: Outermost {...} with comma fix
    try:
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start >= 0 and end > start:
            candidate = cleaned[start:end + 1]
            fixed = re.sub(r',(\s*[}\]])', r'\1', candidate)
            return json.loads(fixed)
    except Exception:
        pass

    # Try 5: Fix common quote issues (smart quotes, etc.)
    try:
        fixed = cleaned.replace('"', '"').replace('"', '"').replace("'", "'").replace("'", "'")
        fixed = re.sub(r',(\s*[}\]])', r'\1', fixed)
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# VISION CONTENT BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def build_vision_content(texts, images, prompt_text):
    """Build interleaved image + text content for vision-mode Claude calls."""
    content = []
    all_pages = sorted(set(list(texts.keys()) + list(images.keys())))
    for pg in all_pages:
        content.append({"type": "text", "text": f"--- Page {pg} ---"})
        if pg in images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": images[pg]}
            })
        if pg in texts:
            content.append({"type": "text", "text": f"[Text]:\n{texts[pg][:3000]}"})
    content.append({"type": "text", "text": prompt_text})
    return content


# ═══════════════════════════════════════════════════════════════════════════════
# TWO-PASS EXTRACTION WITH SMART VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_with_claude(client, texts, images, model, use_vision=True,
                        skip_validation=False, cost_tracker=None, max_retries=2,
                        use_cache=True):
    """
    Two-pass extraction with retry logic and adaptive validation.

    Pass 1: Extract structured JSON from page content (vision + text)
    Pass 2: Validate Pass 1 output against source, return corrections

    Validation pass is automatically skipped if:
    - skip_validation=True (explicit flag)
    - Pass 1 returned 0 plans (nothing to validate)
    - Pass 1 has all roll-forward math correct (high confidence)

    Args:
        client: anthropic.Anthropic client instance
        texts: dict {page_num: text_content}
        images: dict {page_num: base64_jpeg_data}
        model: Claude model identifier
        use_vision: if True and images present, use vision-mode (recommended)
        skip_validation: if True, skip Pass 2 entirely
        cost_tracker: optional CostTracker instance
        max_retries: number of times to retry Pass 1 on failure

    Returns:
        dict matching OUTPUT_SCHEMA, or {"error": "...", "details": "..."} on failure
    """
    combined = ""
    for pg in sorted(texts.keys()):
        combined += f"\n{'=' * 40} PAGE {pg} {'=' * 40}\n{texts[pg]}\n"

    # ── Cache lookup (covers full two-pass result for these inputs) ──────
    vision_mode = "vision" if (use_vision and images) else "text"
    images_fingerprint = ""
    if vision_mode == "vision":
        images_fingerprint = ",".join(
            f"{pg}:{len(img)}" for pg, img in sorted(images.items())
        )
    cache_inputs = (
        model,
        SYSTEM_PROMPT,
        EXTRACTION_PROMPT,
        VALIDATION_PROMPT,
        OUTPUT_SCHEMA,
        vision_mode,
        "no-validate" if skip_validation else "validate",
        combined,
        images_fingerprint,
    )
    if use_cache:
        cached = cache.get("extractions", *cache_inputs)
        if cached is not None:
            log(f"  ⚡ Cache hit — returning cached extraction ({len(cached.get('plans', []))} plan(s))")
            return cached
    else:
        log("  ⓘ Extraction cache bypassed for this job (US filing)")

    # ── PASS 1: EXTRACTION (with retry) ───────────────────────────────────
    log("  Pass 1: Extracting...")
    prompt_text_value = "(see images and text above)" if use_vision and images else combined
    prompt = _safe_format(EXTRACTION_PROMPT, schema=OUTPUT_SCHEMA, text=prompt_text_value)

    if use_vision and images:
        content = build_vision_content(texts, images, prompt)
    else:
        content = prompt

    result1 = None
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            raw1 = call_claude(client, SYSTEM_PROMPT, content, model,
                               max_tokens=EXTRACTION_MAX_TOKENS, cost_tracker=cost_tracker)
            result1 = parse_json_response(raw1)

            if result1 and "plans" in result1:
                break  # Success
            else:
                log(f"  ⚠ Pass 1 attempt {attempt + 1} returned invalid structure")
                if attempt < max_retries:
                    log("  Retrying with stricter format reminder...")
                    reminder = "\n\nIMPORTANT: Return ONLY a valid JSON object starting with { and ending with }. No other text, no markdown."
                    if isinstance(content, str):
                        content = content + reminder
                    else:
                        # Vision content is a list — append a text block
                        content = content + [{"type": "text", "text": reminder}]
        except Exception as e:
            last_error = e
            log(f"  ⚠ Pass 1 attempt {attempt + 1} failed: {e}")
            if attempt < max_retries:
                wait = 2 ** attempt
                log(f"  Retrying in {wait}s...")
                time.sleep(wait)

    if not result1:
        log(f"  ERROR: Pass 1 failed after {max_retries + 1} attempts. Last error: {last_error}")
        # Don't cache failures
        return {"error": "extraction_failed", "details": str(last_error)[:500] if last_error else "invalid_json"}

    plans = result1.get("plans", [])
    log(f"  Pass 1: {len(plans)} plan(s) extracted")

    # Normalize currency on Pass 1 result
    if result1.get("currency"):
        result1["currency"] = normalize_currency(result1["currency"])

    final_result = result1

    if len(plans) == 0:
        log("  ⓘ No plans extracted, skipping validation")
    elif skip_validation:
        log("  ⓘ Validation skipped (--skip-validation flag)")
    else:
        # ── SMART VALIDATION DECISION ─────────────────────────────────────
        # Skip validation if all plans have clean roll-forward math
        needs_validation = False
        arithmetic_issues = []
        for plan in plans:
            warnings = validate_rollforward(plan)
            if warnings:
                needs_validation = True
                arithmetic_issues.extend(warnings)

        if not needs_validation:
            log("  ✓ Pass 1 arithmetic clean — skipping Pass 2 to save cost")
        else:
            # ── PASS 2: VALIDATION ────────────────────────────────────────
            log(f"  Pass 2: Validating ({len(arithmetic_issues)} arithmetic issue(s) detected)...")
            # Send the FULL combined source text — truncating it (previously
            # [:15000]) cut off later pages' roll-forward tables, so Pass 2
            # couldn't find the Pass 1 numbers in its source and stripped them.
            val_prompt = _safe_format(
                VALIDATION_PROMPT,
                text=combined,
                extracted=json.dumps(result1, indent=2),
            )

            try:
                raw2 = call_claude(client, SYSTEM_PROMPT, val_prompt, model,
                                   max_tokens=EXTRACTION_MAX_TOKENS, cost_tracker=cost_tracker)
                result2 = parse_json_response(raw2)

                if result2 and "plans" in result2:
                    if len(result2.get("plans", [])) < len(plans):
                        # Validation must correct values, never destroy plans.
                        # Fewer plans than Pass 1 = it dropped data → keep Pass 1.
                        log(f"  ⚠ Pass 2 returned {len(result2.get('plans', []))} plan(s) vs "
                            f"{len(plans)} in Pass 1 — discarding Pass 2, keeping Pass 1 result")
                    else:
                        log(f"  Pass 2: {len(result2.get('plans', []))} plan(s) validated")
                        if result2.get("currency"):
                            result2["currency"] = normalize_currency(result2["currency"])
                        final_result = result2
                else:
                    log("  ⚠ Pass 2 returned invalid structure, using Pass 1 result")
            except Exception as e:
                log(f"  ⚠ Pass 2 failed: {e}. Using Pass 1 result")

    # Persist to disk cache (only successful results; skipped when use_cache=False)
    if use_cache:
        cache.set("extractions", final_result, *cache_inputs)
    return final_result


# ═══════════════════════════════════════════════════════════════════════════════
# ARITHMETIC VALIDATION (PER-PLAN)
# ═══════════════════════════════════════════════════════════════════════════════

def validate_rollforward(plan: dict) -> list:
    """
    Arithmetic validation for a single plan's roll-forward.
    Returns list of warning messages (empty if all checks pass).

    Standard formula:
        opening + granted - exercised - forfeited_or_lapsed = closing

    Tries multiple formulas to handle different plan conventions:
    - Standard (above)
    - With vested deduction
    - With settled-in-cash deduction
    - Combination
    """
    warnings = []

    opening = plan.get("opening_balance")
    granted = plan.get("granted") or 0
    exercised = plan.get("exercised") or 0
    lapsed = plan.get("forfeited_or_lapsed") or 0
    vested = plan.get("vested") or 0
    settled = plan.get("settled_in_cash") or 0
    closing = plan.get("closing_balance")

    # Current year roll-forward
    if opening is not None and closing is not None:
        # Try multiple formulas (different plans use different conventions)
        candidates = [
            ("standard", opening + granted - exercised - lapsed),
            ("with_vested", opening + granted - exercised - lapsed - vested),
            ("with_settled", opening + granted - exercised - lapsed - settled),
            ("with_vested_settled", opening + granted - exercised - lapsed - vested - settled),
        ]

        threshold = max(abs(closing) * 0.01, 1)  # 1% tolerance or 1 unit

        # Check if ANY formula matches
        matched = False
        min_diff = float('inf')
        min_formula = None
        for formula_name, expected in candidates:
            diff = abs(expected - closing)
            if diff <= threshold:
                matched = True
                break
            if diff < min_diff:
                min_diff = diff
                min_formula = formula_name

        if not matched:
            warnings.append(
                f"Roll-forward mismatch: {opening} + {granted} - {exercised} "
                f"- {lapsed} ≠ {closing} (best diff: {min_diff} using {min_formula})"
            )

    # Prior year validation
    py = plan.get("prior_year")
    if py and isinstance(py, dict):
        po = py.get("opening_balance")
        pg = py.get("granted") or 0
        pe = py.get("exercised") or 0
        pl = py.get("forfeited_or_lapsed") or 0
        pv = py.get("vested") or 0
        pc = py.get("closing_balance")

        if po is not None and pc is not None:
            candidates = [
                po + pg - pe - pl,
                po + pg - pe - pl - pv,
            ]
            threshold = max(abs(pc) * 0.01, 1)

            if not any(abs(exp - pc) <= threshold for exp in candidates):
                warnings.append(
                    f"Prior year roll-forward mismatch: {po} + {pg} - {pe} "
                    f"- {pl} ≠ {pc}"
                )

        # Cross-check: prior year closing should match current year opening
        if pc is not None and opening is not None:
            if abs(pc - opening) > max(abs(opening) * 0.01, 1):
                warnings.append(
                    f"Prior year closing ({pc}) doesn't match current opening ({opening})"
                )

    # Sanity checks
    if exercised < 0 or lapsed < 0 or vested < 0:
        warnings.append(
            f"Negative values found: exercised={exercised}, lapsed={lapsed}, vested={vested} "
            f"(should be POSITIVE per schema convention)"
        )

    exercisable = plan.get("exercisable_at_period_end")
    if exercisable is not None and closing is not None:
        if exercisable > closing:
            warnings.append(
                f"Exercisable ({exercisable}) exceeds closing balance ({closing})"
            )

    return warnings


# ═══════════════════════════════════════════════════════════════════════════════
# ARITHMETIC VALIDATION (ALL PLANS)
# ═══════════════════════════════════════════════════════════════════════════════

def validate_all_plans(data: dict) -> dict:
    """Run roll-forward validation on every plan; attach warnings to output."""
    all_warnings = []
    for i, plan in enumerate(data.get("plans", [])):
        name = plan.get("plan_name", f"Plan {i + 1}")
        for w in validate_rollforward(plan):
            all_warnings.append(f"[{name}] {w}")

    if all_warnings:
        data["_validation_warnings"] = all_warnings
        for w in all_warnings:
            log(f"  ⚠ {w}")
    else:
        log("  ✓ All roll-forward checks passed")
    return data


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL OUTPUT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def validate_final_output(data: dict) -> dict:
    """
    Validate the final merged output for completeness and consistency.
    Adds _validation_summary to the output with quality metrics.
    """
    summary = {
        "total_plans": 0,
        "plans_with_complete_rollforward": 0,
        "plans_with_valuation_inputs": 0,
        "plans_with_tranches": 0,
        "plans_with_prior_year": 0,
        "plans_with_exercise_price": 0,
        "plans_cash_settled": 0,
        "plans_nil_cost": 0,
        "warnings": [],
    }

    plans = data.get("plans", [])
    summary["total_plans"] = len(plans)

    for plan in plans:
        plan_name = plan.get("plan_name", "Unknown")

        # Completeness checks
        has_rollforward = (
            plan.get("opening_balance") is not None
            and plan.get("closing_balance") is not None
        )
        if has_rollforward:
            summary["plans_with_complete_rollforward"] += 1

        # Valuation inputs check
        vi = plan.get("valuation_inputs")
        if vi and isinstance(vi, dict):
            has_vi = any(vi.get(k) is not None for k in [
                "volatility_pct", "risk_free_rate_pct", "fair_value_per_option"
            ])
            if has_vi:
                summary["plans_with_valuation_inputs"] += 1

        # Tranches check
        if plan.get("tranches"):
            summary["plans_with_tranches"] += 1

        # Prior year check
        py = plan.get("prior_year")
        if py and isinstance(py, dict):
            has_py = any(py.get(k) is not None for k in ["opening_balance", "closing_balance"])
            if has_py:
                summary["plans_with_prior_year"] += 1

        # Exercise price check
        if plan.get("weighted_avg_exercise_price") is not None:
            summary["plans_with_exercise_price"] += 1

        # Cash-settled / nil-cost flags
        if plan.get("is_cash_settled") is True:
            summary["plans_cash_settled"] += 1
        if plan.get("is_nil_cost") is True:
            summary["plans_nil_cost"] += 1

        # Run arithmetic validation
        plan_warnings = validate_rollforward(plan)
        for w in plan_warnings:
            summary["warnings"].append(f"[{plan_name}] {w}")

    # Check metadata completeness
    if not data.get("company_name"):
        summary["warnings"].append("Missing company_name")
    if not data.get("report_period"):
        summary["warnings"].append("Missing report_period")
    if not data.get("currency"):
        summary["warnings"].append("Missing currency")

    data["_validation_summary"] = summary

    # Log summary
    log(f"\n📊 Validation Summary:")
    log(f"   Total plans:                    {summary['total_plans']}")
    log(f"   With complete roll-forward:     {summary['plans_with_complete_rollforward']}")
    log(f"   With valuation inputs:          {summary['plans_with_valuation_inputs']}")
    log(f"   With tranche details:           {summary['plans_with_tranches']}")
    log(f"   With prior year:                {summary['plans_with_prior_year']}")
    log(f"   With exercise price:            {summary['plans_with_exercise_price']}")
    if summary["plans_cash_settled"]:
        log(f"   Cash-settled plans:             {summary['plans_cash_settled']}")
    if summary["plans_nil_cost"]:
        log(f"   Nil-cost plans:                 {summary['plans_nil_cost']}")
    if summary["warnings"]:
        log(f"   ⚠ Warnings:                     {len(summary['warnings'])}")

    return data


# ═══════════════════════════════════════════════════════════════════════════════
# PER-BATCH RESULT MERGING (DEEP MERGE)
# ═══════════════════════════════════════════════════════════════════════════════

def merge_results(results: list) -> dict:
    """
    Merge per-batch extraction results into a single output.

    Strategy:
    - Metadata (company_name, period, currency, reporting_standard): first non-null
    - Plans: deduplicate by (plan_name lowercase, plan_type uppercase)
    - Plan fields: prefer non-null values from any batch
    - Tranches: append by grant_date (no duplicates)
    - valuation_inputs / prior_year: deep merge (preserve non-null values)
    """
    merged = {
        "company_name": None,
        "report_period": None,
        "currency": None,
        "reporting_standard": None,
        "plans": []
    }
    seen = {}

    for r in results:
        if not r or "error" in r:
            continue

        # Metadata: take first non-null
        for meta_field in ["company_name", "report_period", "currency", "reporting_standard"]:
            if not merged.get(meta_field) and r.get(meta_field):
                merged[meta_field] = r[meta_field]

        # Plans: merge by (name, type) key (case-insensitive)
        for plan in r.get("plans", []):
            key = (
                (plan.get("plan_name") or "").strip().lower(),
                (plan.get("plan_type") or "").strip().upper()
            )

            if key in seen:
                existing = seen[key]
                # Merge fields: take non-null values, preserve original where possible
                for k, v in plan.items():
                    if k == "tranches":
                        # Append tranches if not already present (by grant_date)
                        existing_tranches = existing.get("tranches") or []
                        new_tranches = v or []
                        existing_dates = {
                            t.get("grant_date") for t in existing_tranches
                            if t.get("grant_date")
                        }
                        for t in new_tranches:
                            if t.get("grant_date") not in existing_dates:
                                existing_tranches.append(t)
                        if existing_tranches:
                            existing["tranches"] = existing_tranches

                    elif k == "valuation_inputs":
                        # Deep merge valuation_inputs object
                        if v and isinstance(v, dict):
                            existing_vi = existing.get("valuation_inputs") or {}
                            for vi_k, vi_v in v.items():
                                if vi_v is not None and existing_vi.get(vi_k) is None:
                                    existing_vi[vi_k] = vi_v
                            existing["valuation_inputs"] = existing_vi

                    elif k == "prior_year":
                        # Deep merge prior_year object
                        if v and isinstance(v, dict):
                            existing_py = existing.get("prior_year") or {}
                            for py_k, py_v in v.items():
                                if py_v is not None and existing_py.get(py_k) is None:
                                    existing_py[py_k] = py_v
                            existing["prior_year"] = existing_py

                    else:
                        # Take non-null value
                        if v is not None and existing.get(k) is None:
                            existing[k] = v
            else:
                seen[key] = plan
                merged["plans"].append(plan)

    # Normalize currency on merged result
    if merged.get("currency"):
        merged["currency"] = normalize_currency(merged["currency"])

    return merged