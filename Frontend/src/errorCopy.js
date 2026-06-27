// Single source of truth for user-facing error copy.
//
// Non-technical users must NEVER see raw exception text, URLs, stack traces, or
// internal codes. The backend tags failures with a stable `error_code` (+ context);
// this module turns that into a friendly message and decides how to show it:
//   variant: 'modal'  -> centered popup (terminal outcomes the user should read)
//   variant: 'banner' -> gentle inline notice (quick, fixable inputs)
// When no code is present (plain HTTP errors), we infer one from the raw string.

const CONTACT_DEV = 'Please contact your developer.'

function who(ctx = {}) {
  return (ctx.company || '').trim() || (ctx.ticker || '').trim()
}

function yearPhrase(ctx = {}) {
  const y = (ctx.year ?? '').toString().trim()
  if (!y) return ''
  return /^\d{4}$/.test(y) ? `FY${y}` : y
}

function friendlyInput(raw = '') {
  const r = (raw || '').toLowerCase()
  if (r.includes('only pdf')) return 'Please upload a PDF file.'
  if (r.includes('file too large')) return 'That file is too large. Please upload a PDF under 100 MB.'
  if (r.includes('required')) return 'Please enter a company name or ticker symbol.'
  return 'Please check your details and try again.'
}

// code -> builder(ctx, raw) -> { variant, title, body }
const COPY = {
  NO_PAGES: (ctx) => {
    const name = who(ctx)
    const yr = yearPhrase(ctx)
    let body
    if (name && yr) {
      body = `We couldn't find any stock-option or share-based-payment data for ${name} in the ${yr} report.`
    } else if (name) {
      body = `We couldn't find any stock-option or share-based-payment data for ${name} in the provided report.`
    } else {
      body = `We couldn't find any stock-option or share-based-payment data in the provided report.`
    }
    return { variant: 'modal', title: 'No options data available', body }
  },

  NO_REPORT: (ctx) => {
    const name = who(ctx) || 'this company'
    return {
      variant: 'modal',
      title: 'Report not found',
      body: `We couldn't find an annual report for ${name}. Please double-check the name or ticker symbol, or upload the report PDF directly using the Upload tab.`,
    }
  },

  EDINET_DOWN: () => ({
    variant: 'modal',
    title: 'Japan (EDINET) temporarily unavailable',
    body: "Japan's official EDINET service is currently experiencing an issue. Please try again after some time.",
  }),

  NOT_FOUND: (ctx) => {
    const name = who(ctx) || 'that company'
    return {
      variant: 'modal',
      title: 'Company not found',
      body: `We couldn't find ${name}. Please check the spelling, or try the company's full legal name.`,
    }
  },

  EXPIRED: () => ({
    variant: 'modal',
    title: 'Session expired',
    body: 'This session has expired. Please submit your request again.',
  }),

  CONFIG: () => ({
    variant: 'banner',
    title: 'Service needs attention',
    body: `This service needs attention. ${CONTACT_DEV}`,
  }),

  BAD_INPUT: (ctx, raw) => ({
    variant: 'banner',
    title: 'Check your details',
    body: friendlyInput(raw),
  }),

  UNKNOWN: () => ({
    variant: 'banner',
    title: 'Something went wrong',
    body: `Something went wrong while processing your request. Please try again — if it keeps happening, ${CONTACT_DEV}`,
  }),
}

// Infer a code from a raw HTTP/exception string when the backend didn't tag one.
function codeFromRaw(raw = '') {
  const r = (raw || '').toLowerCase()
  if (!r) return 'UNKNOWN'
  if (r.includes('no relevant pages')) return 'NO_PAGES'
  if (r.includes('anthropic') || r.includes('together_api') || r.includes('api key') || r.includes('api_key')) return 'CONFIG'
  if (r.includes('only pdf') || r.includes('file too large') || r.includes('is required') || r.includes('are accepted')) return 'BAD_INPUT'
  if (r.includes('could not find a') && r.includes('company for')) return 'NOT_FOUND'
  if (r.includes('job not found') || r.includes('session has expired')) return 'EXPIRED'
  if (
    r.includes('annual report') || r.includes('ir site') || r.includes('possibly-wrong') ||
    r.includes('fetch failed') || r.includes('resolve failed') || r.includes('could not fetch') ||
    r.includes('no gate-passing')
  ) return 'NO_REPORT'
  return 'UNKNOWN'
}

// Main entry. Pass the backend code + context when available; otherwise the raw
// message. Returns { variant: 'modal' | 'banner', title, body }.
export function friendlyError({ code, context, raw } = {}) {
  let c = code
  if (!c || !COPY[c]) c = codeFromRaw(raw)
  const build = COPY[c] || COPY.UNKNOWN
  return { ...build(context || {}, raw || ''), code: c }
}
