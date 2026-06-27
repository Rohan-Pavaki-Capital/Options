import { useState, useRef, useCallback, useEffect } from 'react'
import {
  CloudUpload, FileUp, Search, Link2,
  AlertCircle, Loader2, Download, Check,
  FlaskConical, ChevronDown,
} from 'lucide-react'

const API_BASE = `${import.meta.env.VITE_API_BASE || ''}/api`
const GURU_RE = /gurufocus\.com\/stock\//i

// Resolve a GuruFocus stock URL -> {company_name, ticker, exchange, country, european, is_us}.
async function resolveGuruUrl(url) {
  try {
    const res = await fetch(`${API_BASE}/gurufocus-resolve?url=${encodeURIComponent(url)}`)
    if (!res.ok) return null
    return await res.json()
  } catch {
    return null
  }
}

// Reusable "paste a GuruFocus link" row, shown on every market tab. Parses the
// ticker + market from the URL (and the company name, except for US tickers) and
// hands the result to `onApply`, which the host panel uses to run its own fetch.
function GuruFocusLink({ onApply, uploading, expectCountry }) {
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const isGuru = GURU_RE.test(url.trim())

  const go = useCallback(async () => {
    if (!isGuru || busy || uploading) return
    setBusy(true); setMsg(null)
    try {
      const g = await resolveGuruUrl(url.trim())
      if (!g) { setMsg("Couldn't read that GuruFocus link — check the URL."); return }
      if (expectCountry && g.country && g.country !== expectCountry) {
        setMsg(`That link looks like a ${g.country} listing, not ${expectCountry}. It may not resolve on this tab.`)
        // still apply — the ticker is usually what matters; the user was warned.
      }
      onApply(g)
    } finally {
      setBusy(false)
    }
  }, [url, isGuru, busy, uploading, expectCountry, onApply])

  return (
    <div className="mb-4">
      <label className="field-label">
        Or paste a GuruFocus link <span className="text-gray-400 font-normal">(auto-fills ticker &amp; company)</span>
      </label>
      <div className="flex gap-2">
        <div className="relative flex-1">
          <Link2 className="w-4 h-4 text-gray-400 absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            value={url}
            onChange={(e) => { setUrl(e.target.value); setMsg(null) }}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); go() } }}
            placeholder="gurufocus.com/stock/NAS:AAPL/summary"
            disabled={uploading}
            className="field-input pl-9"
          />
        </div>
        <button
          type="button"
          onClick={go}
          disabled={!isGuru || busy || uploading}
          className="btn btn-ghost flex-shrink-0"
        >
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Use link'}
        </button>
      </div>
      {msg && (
        <div className="text-xs text-amber-700 mt-1 flex items-start gap-1">
          <AlertCircle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />{msg}
        </div>
      )}
    </div>
  )
}

// Config for the simple ticker+name markets rendered by <SourcePanel>. Markets
// with bespoke UIs (EDGAR, UK, Denmark, Korea, EU, Canada, Germany) keep their
// own panels above.
const SIMPLE_MARKETS = {
  japan: {
    endpoint: 'extract-from-japan', errLabel: 'Japan',
    title: 'Japan',
    desc: "Enter the company name (and TSE ticker, e.g. 7203, if you have it). We search the company's own investor-relations site for the latest annual report first, then fall back to EDINET when available.",
    tickerLabel: 'Ticker (TSE code)', placeholder: '7203', filing: 'Annual report',
    footer: 'Japan-listed companies · IR site first, then EDINET (FSA) · Japanese reports detected automatically, results in English',
  },
  china: {
    endpoint: 'extract-from-china', errLabel: 'China (CNINFO)',
    title: 'China',
    desc: "Enter a 6-digit stock code (e.g. 600519) or company name. We'll pull the latest 年度报告 (annual report).",
    tickerLabel: 'Ticker (stock code)', placeholder: '600519', filing: '年度报告 (annual)',
    footer: 'SSE / SZSE / BSE listed companies · Powered by CNINFO · Chinese reports detected automatically, results in English',
  },
  india: {
    endpoint: 'extract-from-india', errLabel: 'India (BSE)',
    title: 'India',
    desc: "Enter an NSE/BSE ticker (e.g. RELIANCE), BSE scrip code, ISIN, or company name. We'll pull the latest annual report.",
    tickerLabel: 'Ticker / scrip code', placeholder: 'RELIANCE', filing: 'Annual report',
    footer: 'India-listed companies · Powered by BSE · ESOP / share-based payment notes (IND AS 102)',
  },
  hongkong: {
    endpoint: 'extract-from-hongkong', errLabel: 'Hong Kong (HKEXnews)',
    title: 'Hong Kong',
    desc: "Enter a HK stock code (e.g. 700) or company name. We'll pull the latest annual report.",
    tickerLabel: 'Ticker (stock code)', placeholder: '700', filing: 'Annual report',
    footer: 'HK-listed companies · Powered by HKEXnews · English / Chinese reports detected automatically',
  },
  taiwan: {
    endpoint: 'extract-from-taiwan', errLabel: 'Taiwan (TWSE)',
    title: 'Taiwan',
    desc: "Enter a 4-digit TWSE code (e.g. 2330) or company name. We'll pull the latest annual consolidated financial statements.",
    tickerLabel: 'Ticker (TWSE code)', placeholder: '2330', filing: '合併財務報告 (annual)',
    footer: 'Taiwan-listed companies · Powered by TWSE / MOPS · Chinese reports detected automatically, results in English',
  },
  brazil: {
    endpoint: 'extract-from-brazil', errLabel: 'Brazil (CVM)',
    title: 'Brazil',
    desc: "Enter a B3 ticker (e.g. PETR4), CNPJ, or company name. We'll pull the latest DFP annual financial statements.",
    tickerLabel: 'Ticker / CNPJ', placeholder: 'PETR4', filing: 'DFP (annual)',
    footer: 'Brazil-listed companies · Powered by CVM open data · Portuguese reports detected automatically, results in English',
  },
  indonesia: {
    endpoint: 'extract-from-indonesia', errLabel: 'Indonesia (IDX)',
    title: 'Indonesia',
    desc: "Enter an IDX ticker code (e.g. BBCA, GOTO). We'll pull the latest audited annual financial statements.",
    tickerLabel: 'Ticker (kodeEmiten)', placeholder: 'BBCA', filing: 'Audited financial statements',
    footer: 'Indonesia-listed companies · Powered by IDX · Bahasa / English reports detected automatically',
    requireTicker: true,
  },
  malaysia: {
    endpoint: 'extract-from-malaysia', errLabel: 'Malaysia (Bursa)',
    title: 'Malaysia',
    desc: "Enter a Bursa stock code (e.g. 1155), a short-name ticker (e.g. MAYBANK), or company name. We'll pull the latest annual-report financial statements.",
    tickerLabel: 'Ticker / stock code', placeholder: '1155', filing: 'Annual financial statements',
    footer: 'Malaysia-listed companies · Powered by Bursa Malaysia · Share-based payment notes (MFRS 2)',
  },
  thailand: {
    endpoint: 'extract-from-thailand', errLabel: 'Thailand (SEC 56-1)',
    title: 'Thailand',
    desc: "Enter a company name (e.g. PTT, Delta Electronics) or a major-issuer ticker. We'll pull the latest SEC 56-1 One Report. Company name resolves most reliably.",
    tickerLabel: 'Company name / ticker', placeholder: 'PTT', filing: '56-1 One Report (annual)',
    footer: 'Thailand-listed companies · Powered by SEC Thailand (iDisc) · Thai / English reports detected automatically, results in English',
  },
  israel: {
    endpoint: 'extract-from-israel', errLabel: 'Israel (TASE MAYA)',
    title: 'Israel',
    desc: "Enter a MAYA companyId (e.g. 604), a TASE ticker (e.g. LUMI), or a major-issuer name. We'll pull the latest annual / periodic financial statements.",
    tickerLabel: 'companyId / ticker', placeholder: '604', filing: 'Annual / periodic statements',
    footer: 'TASE-listed companies · Powered by TASE-MAYA (via Firecrawl) · Hebrew reports detected automatically · Name search covers major issuers — the numeric companyId (in the maya.tase.co.il/en/companies/<id> URL) works for any company',
  },
}

export default function UploadScreen({
  mode,
  onUpload,
  onFetchDiamond,
  onFetchEdgar,
  onFetchUk,
  onFetchDenmark,
  onFetchKorea,
  onFetchEu,
  onSearchEu,
  onResolveGuru,
  onFetchCanada,
  onFetchGermany,
  onFetchSingapore,
  onFetchMexico,
  onFetchSimple,
  onFetchTest,
  uploading,
  error,
}) {
  return (
    <div>
      {error && (
        <div className="max-w-xl mx-auto mb-5 px-4 py-3 bg-red-50/80 border border-red-200 rounded-md flex items-start gap-2.5 text-[13px] text-red-800 leading-relaxed">
          <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0 text-red-500" />
          <span>{error}</span>
        </div>
      )}

      {mode === 'diamond' ? (
        <DiamondPanel onFetchDiamond={onFetchDiamond} uploading={uploading} />
      ) : mode === 'testing' ? (
        <TestingPanel onFetchTest={onFetchTest} uploading={uploading} />
      ) : mode === 'upload' ? (
        <UploadPanel onUpload={onUpload} uploading={uploading} />
      ) : mode === 'edgar' ? (
        <EdgarPanel onFetchEdgar={onFetchEdgar} uploading={uploading} />
      ) : mode === 'uk' ? (
        <UkPanel onFetchUk={onFetchUk} uploading={uploading} />
      ) : mode === 'denmark' ? (
        <DenmarkPanel onFetchDenmark={onFetchDenmark} uploading={uploading} />
      ) : mode === 'korea' ? (
        <KoreaPanel onFetchKorea={onFetchKorea} uploading={uploading} />
      ) : mode === 'eu' ? (
        <EuropePanel onFetchEu={onFetchEu} onSearchEu={onSearchEu} onResolveGuru={onResolveGuru} uploading={uploading} />
      ) : mode === 'canada' ? (
        <CanadaPanel onFetchCanada={onFetchCanada} onUpload={onUpload} uploading={uploading} />
      ) : mode === 'germany' ? (
        <GermanyPanel onFetchGermany={onFetchGermany} onUpload={onUpload} uploading={uploading} />
      ) : mode === 'singapore' ? (
        <SingaporePanel onFetchSingapore={onFetchSingapore} uploading={uploading} />
      ) : mode === 'mexico' ? (
        <MexicoPanel onFetchMexico={onFetchMexico} uploading={uploading} />
      ) : SIMPLE_MARKETS[mode] ? (
        <SourcePanel cfg={SIMPLE_MARKETS[mode]} onFetchSimple={onFetchSimple} uploading={uploading} />
      ) : null}
    </div>
  )
}

function UploadPanel({ onUpload, uploading }) {
  const [isDragging, setIsDragging] = useState(false)
  const fileInputRef = useRef(null)

  const handleFile = useCallback((file) => {
    if (!file) return
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      alert('Please upload a PDF file')
      return
    }
    if (file.size > 100 * 1024 * 1024) {
      alert('File too large (max 100 MB)')
      return
    }
    onUpload(file)
  }, [onUpload])

  const handleDrop = useCallback((e) => {
    e.preventDefault()
    setIsDragging(false)
    handleFile(e.dataTransfer.files[0])
  }, [handleFile])

  return (
    <div
      onDrop={handleDrop}
      onDragOver={(e) => { e.preventDefault(); setIsDragging(true) }}
      onDragLeave={() => setIsDragging(false)}
      className={`
        max-w-xl mx-auto border border-dashed rounded-lg p-10 text-center transition-colors duration-150
        ${isDragging ? 'border-brand/60 bg-brand-pale/60' : 'border-gray-300 bg-paper hover:border-gray-400'}
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <div className="w-11 h-11 mx-auto mb-4 rounded-full bg-canvas border border-hairline flex items-center justify-center">
        {uploading
          ? <Loader2 className="w-5 h-5 text-brand animate-spin" />
          : <CloudUpload className="w-5 h-5 text-gray-400" />}
      </div>

      <div className="text-[15px] font-medium text-ink mb-1">
        {uploading ? 'Uploading…' : 'Drop an annual report here'}
      </div>
      <div className="text-[13px] text-gray-500 mb-5 leading-relaxed">
        10-K, annual report, or financial statements PDF.
        Share-based compensation data is extracted automatically.
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept=".pdf"
        onChange={(e) => handleFile(e.target.files?.[0])}
        className="hidden"
      />
      <button
        onClick={() => fileInputRef.current?.click()}
        disabled={uploading}
        className="btn btn-primary"
      >
        <FileUp className="w-4 h-4" />
        {uploading ? 'Processing…' : 'Browse files'}
      </button>

      <div className="text-[11px] text-gray-400 mt-4 tracking-wide">
        PDF · max 100 MB · up to 500 pages
      </div>
    </div>
  )
}

// Countries with a dedicated data-API source (match backend COUNTRY_TO_SOURCE).
// Picking one routes Diamond to that authoritative feed (e.g. United States -> SEC
// EDGAR); leaving it blank uses the universal IR-scraper.
const DIAMOND_COUNTRIES = [
  'United States', 'Canada', 'United Kingdom', 'Japan', 'South Korea',
  'China', 'Hong Kong', 'Taiwan', 'India', 'Indonesia', 'Brazil',
  'Israel', 'Denmark',
  'France', 'Netherlands', 'Spain', 'Italy', 'Sweden', 'Finland',
  'Belgium', 'Austria', 'Portugal', 'Poland', 'Greece', 'Luxembourg',
  'Norway', 'Iceland', 'Croatia', 'Hungary', 'Romania', 'Slovakia',
  'Slovenia', 'Estonia', 'Latvia', 'Lithuania', 'Cyprus', 'Malta',
]

// Broader list for the Testing tab: the scraper uses country only as a free-text
// hint for the resolver, so any market is useful (not just dedicated-API ones).
// Includes the Gulf/MENA + other markets that have NO dedicated feed.
const TESTING_COUNTRIES = [
  'United Arab Emirates', 'Saudi Arabia', 'Qatar', 'Kuwait', 'Bahrain', 'Oman',
  'Egypt', 'Jordan', 'Turkey', 'South Africa', 'Nigeria', 'Kenya',
  'United States', 'Canada', 'United Kingdom', 'Ireland', 'Germany', 'France',
  'Netherlands', 'Spain', 'Italy', 'Switzerland', 'Sweden', 'Norway', 'Denmark',
  'Finland', 'Belgium', 'Austria', 'Portugal', 'Poland', 'Greece', 'Luxembourg',
  'Japan', 'South Korea', 'China', 'Hong Kong', 'Taiwan', 'India', 'Indonesia',
  'Singapore', 'Malaysia', 'Thailand', 'Philippines', 'Vietnam', 'Pakistan',
  'Australia', 'New Zealand', 'Brazil', 'Mexico', 'Argentina', 'Chile', 'Israel',
]

// EU/EEA markets for the Europe tab's optional Market hint. Used as a free-text
// country hint for the IR scraper (full names scrape better than ISO codes).
// Germany & Ireland are included: they have no ESEF filing, but the scraper —
// which now runs first — can still reach their own IR sites.
const EU_MARKETS = [
  'Germany', 'France', 'Netherlands', 'Spain', 'Italy', 'Ireland',
  'Sweden', 'Finland', 'Denmark', 'Norway', 'Belgium', 'Austria',
  'Portugal', 'Poland', 'Greece', 'Luxembourg', 'Iceland', 'Croatia',
  'Hungary', 'Romania', 'Slovakia', 'Slovenia', 'Estonia', 'Latvia',
  'Lithuania', 'Cyprus', 'Malta',
]

// Searchable, alphabetically-sorted country picker used by the Diamond, Testing
// and Europe tabs. Behaves like a <select> (value + onChange) but adds a search box
// and a clearable "Auto-detect" option. value "" = no country chosen.
function CountrySelect({ options, value, onChange, placeholder = 'Auto-detect', disabled }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const ref = useRef(null)

  const sorted = [...options].sort((a, b) => a.localeCompare(b))
  const needle = q.trim().toLowerCase()
  const filtered = needle ? sorted.filter((c) => c.toLowerCase().includes(needle)) : sorted

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => {
      if (ref.current && !ref.current.contains(e.target)) { setOpen(false); setQ('') }
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  const pick = (c) => { onChange(c); setOpen(false); setQ('') }

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        className="field-input w-full flex items-center justify-between text-left gap-2"
      >
        <span className={value ? 'text-ink truncate' : 'text-gray-400 truncate'}>
          {value || placeholder}
        </span>
        <ChevronDown className="w-4 h-4 text-gray-400 flex-shrink-0" />
      </button>

      {open && (
        <div className="absolute z-20 mt-1 w-full bg-white border border-gray-200 rounded-md shadow-lg">
          <div className="p-2 border-b border-gray-100">
            <div className="relative">
              <Search className="w-4 h-4 text-gray-400 absolute left-2.5 top-1/2 -translate-y-1/2" />
              <input
                autoFocus
                type="text"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search country…"
                className="w-full pl-8 pr-2 py-1.5 text-sm rounded border border-gray-300 focus:outline-none focus:ring-2 focus:ring-brand focus:border-brand"
              />
            </div>
          </div>
          <ul className="max-h-56 overflow-auto py-1">
            <li>
              <button
                type="button"
                onClick={() => pick('')}
                className="w-full text-left px-3 py-1.5 text-sm hover:bg-brand-pale flex items-center justify-between"
              >
                <span className="text-gray-500">{placeholder}</span>
                {!value && <Check className="w-3.5 h-3.5 text-brand flex-shrink-0" />}
              </button>
            </li>
            {filtered.map((c) => (
              <li key={c}>
                <button
                  type="button"
                  onClick={() => pick(c)}
                  className="w-full text-left px-3 py-1.5 text-sm text-gray-900 hover:bg-brand-pale flex items-center justify-between"
                >
                  <span className="truncate">{c}</span>
                  {value === c && <Check className="w-3.5 h-3.5 text-brand flex-shrink-0" />}
                </button>
              </li>
            ))}
            {filtered.length === 0 && (
              <li className="px-3 py-2 text-sm text-gray-400">No match</li>
            )}
          </ul>
        </div>
      )}
    </div>
  )
}

function DiamondPanel({ onFetchDiamond, uploading }) {
  const [companyName, setCompanyName] = useState('')
  const [ticker, setTicker] = useState('')
  const [country, setCountry] = useState('')

  // Either field is enough; both is best for disambiguation.
  const canSubmit = (companyName.trim().length > 0 || ticker.trim().length > 0) && !uploading

  const handleSubmit = useCallback((e) => {
    e?.preventDefault?.()
    if (!canSubmit) return
    onFetchDiamond({
      company_name: companyName.trim(),
      ticker: ticker.trim(),
      country: country.trim(),
    })
  }, [companyName, ticker, country, canSubmit, onFetchDiamond])

  return (
    <form
      onSubmit={handleSubmit}
      className={`
        max-w-xl mx-auto panel p-7
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <PanelHead
        eyebrow="Any market · Auto-sourced"
        title="Search any company"
        desc="Enter a company name and ticker. We locate the latest annual report and extract the share-based payment disclosures."
      />

      <div className="grid grid-cols-2 gap-3 mb-5">
        <div>
          <label className="field-label">
            Company name
          </label>
          <input
            type="text"
            value={companyName}
            onChange={(e) => setCompanyName(e.target.value)}
            placeholder="Malayan Banking Berhad"
            disabled={uploading}
            className="field-input"
          />
        </div>
        <div>
          <label className="field-label">
            Ticker
          </label>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            placeholder="MAYBANK"
            disabled={uploading}
            className="field-input"
          />
        </div>
      </div>

      <div className="mb-5">
        <label className="field-label">
          Country <span className="text-gray-400 font-normal">(optional)</span>
        </label>
        <CountrySelect
          options={DIAMOND_COUNTRIES}
          value={country}
          onChange={setCountry}
          placeholder="Auto / other — use investor-relations site"
          disabled={uploading}
        />
        <div className="text-xs text-gray-400 mt-1 leading-relaxed">
          Pick the company's country to use its official exchange feed (e.g. United
          States → SEC EDGAR). Leave blank to fetch from the company's own website.
        </div>
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="btn btn-primary w-full justify-center"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
        {uploading ? 'Finding report…' : 'Find & extract'}
      </button>

      <div className="panel-foot">
        Routes to the best source automatically — dedicated exchange feeds where
        available, the company's own investor-relations site otherwise.
        Best-effort for markets without a dedicated feed.
      </div>
    </form>
  )
}

function SingaporePanel({ onFetchSingapore, uploading }) {
  const [companyName, setCompanyName] = useState('')
  const [ticker, setTicker] = useState('')

  // Either field is enough; both is best for disambiguation.
  const canSubmit = (companyName.trim().length > 0 || ticker.trim().length > 0) && !uploading

  const handleSubmit = useCallback((e) => {
    e?.preventDefault?.()
    if (!canSubmit) return
    onFetchSingapore({
      company_name: companyName.trim(),
      ticker: ticker.trim(),
    })
  }, [companyName, ticker, canSubmit, onFetchSingapore])

  return (
    <form
      onSubmit={handleSubmit}
      className={`
        max-w-xl mx-auto panel p-7
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <PanelHead
        eyebrow="Singapore · SGX"
        title="Singapore"
        desc="Enter the company name and SGX ticker code. We retrieve the latest annual report from the company's investor-relations site and extract the share-based payment disclosures."
      />

      <GuruFocusLink
        uploading={uploading}
        expectCountry="Singapore"
        onApply={(g) => onFetchSingapore({ company_name: g.company_name || '', ticker: g.ticker || '' })}
      />

      <div className="grid grid-cols-2 gap-3 mb-5">
        <div>
          <label className="field-label">
            Company name
          </label>
          <input
            type="text"
            value={companyName}
            onChange={(e) => setCompanyName(e.target.value)}
            placeholder="Singapore Airlines"
            disabled={uploading}
            className="field-input"
          />
        </div>
        <div>
          <label className="field-label">
            SGX ticker code
          </label>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            placeholder="Z77"
            disabled={uploading}
            className="field-input"
          />
        </div>
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="btn btn-primary w-full justify-center"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
        {uploading ? 'Finding report…' : 'Fetch & extract'}
      </button>

      <div className="panel-foot">
        SGX-listed companies · The ticker code is used as <span className="font-mono">SGX:&lt;code&gt;</span> (e.g.
        Z77 → SGX:Z77). Fetches from the company's own investor-relations site —
        best-effort for issuers whose site serves a downloadable annual report.
      </div>
    </form>
  )
}

function MexicoPanel({ onFetchMexico, uploading }) {
  const [companyName, setCompanyName] = useState('')
  const [ticker, setTicker] = useState('')

  // Either field is enough; both is best for disambiguation.
  const canSubmit = (companyName.trim().length > 0 || ticker.trim().length > 0) && !uploading

  const handleSubmit = useCallback((e) => {
    e?.preventDefault?.()
    if (!canSubmit) return
    onFetchMexico({
      company_name: companyName.trim(),
      ticker: ticker.trim(),
    })
  }, [companyName, ticker, canSubmit, onFetchMexico])

  return (
    <form
      onSubmit={handleSubmit}
      className={`
        max-w-xl mx-auto panel p-7
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <PanelHead
        eyebrow="Mexico · BMV"
        title="Mexico"
        desc="Enter the company name and BMV ticker. We retrieve the latest annual report from the company's investor-relations site and extract the share-based payment disclosures."
      />

      <GuruFocusLink
        uploading={uploading}
        expectCountry="Mexico"
        onApply={(g) => onFetchMexico({ company_name: g.company_name || '', ticker: g.ticker || '' })}
      />

      <div className="grid grid-cols-2 gap-3 mb-5">
        <div>
          <label className="field-label">
            Company name
          </label>
          <input
            type="text"
            value={companyName}
            onChange={(e) => setCompanyName(e.target.value)}
            placeholder="Walmart de México"
            disabled={uploading}
            className="field-input"
          />
        </div>
        <div>
          <label className="field-label">
            BMV ticker
          </label>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            placeholder="WALMEX"
            disabled={uploading}
            className="field-input"
          />
        </div>
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="btn btn-primary w-full justify-center"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
        {uploading ? 'Finding report…' : 'Fetch & extract'}
      </button>

      <div className="panel-foot">
        BMV-listed companies · The ticker is used as <span className="font-mono">BMV:&lt;code&gt;</span> (e.g.
        WALMEX → BMV:WALMEX). Fetches from the company's own investor-relations site —
        best-effort for issuers whose site serves a downloadable annual report.
      </div>
    </form>
  )
}

// TESTING — scraper only, no LLM. Enter company name + ticker, fetch the latest
// filing PDF, and report Firecrawl credits + time taken.
function TestingPanel({ onFetchTest, uploading }) {
  const [companyName, setCompanyName] = useState('')
  const [ticker, setTicker] = useState('')
  const [country, setCountry] = useState('')

  const canSubmit = (companyName.trim().length > 0 || ticker.trim().length > 0) && !uploading

  const handleSubmit = useCallback((e) => {
    e?.preventDefault?.()
    if (!canSubmit) return
    onFetchTest({
      company_name: companyName.trim(),
      ticker: ticker.trim(),
      country: country.trim(),
    })
  }, [companyName, ticker, country, canSubmit, onFetchTest])

  return (
    <form
      onSubmit={handleSubmit}
      className={`
        max-w-xl mx-auto panel p-7 border-accent/40
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <PanelHead
        eyebrow="Diagnostic · Fetch only"
        title="Source test"
        desc="Retrieves the latest filing PDF without running extraction. Reports the data-source cost and time taken — useful for checking whether a company's report is reachable."
      />

      <div className="grid grid-cols-2 gap-3 mb-5">
        <div>
          <label className="field-label">
            Company name
          </label>
          <input
            type="text"
            value={companyName}
            onChange={(e) => setCompanyName(e.target.value)}
            placeholder="Bank Leumi"
            disabled={uploading}
            className="field-input"
          />
        </div>
        <div>
          <label className="field-label">
            Ticker
          </label>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            placeholder="LUMI"
            disabled={uploading}
            className="field-input"
          />
        </div>
      </div>

      <div className="mb-5">
        <label className="field-label">
          Country <span className="text-gray-400 font-normal">(optional — improves matching)</span>
        </label>
        <CountrySelect
          options={TESTING_COUNTRIES}
          value={country}
          onChange={setCountry}
          placeholder="Auto-detect"
          disabled={uploading}
        />
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="btn btn-accent w-full justify-center"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <FlaskConical className="w-4 h-4" />}
        {uploading ? 'Fetching…' : 'Fetch PDF only'}
      </button>

      <div className="panel-foot">
        Routes through the same source resolver as Diamond, then stops at the PDF.
        Use this to measure scraper cost (Firecrawl credits) and speed.
      </div>
    </form>
  )
}

function EdgarPanel({ onFetchEdgar, uploading }) {
  const [ticker, setTicker] = useState('')
  const [companyName, setCompanyName] = useState('')
  const [form, setForm] = useState('10-K')

  const canSubmit = ticker.trim().length > 0 && !uploading

  const handleSubmit = useCallback((e) => {
    e?.preventDefault?.()
    if (!canSubmit) return
    onFetchEdgar({
      ticker: ticker.trim().toUpperCase(),
      company_name: companyName.trim() || null,
      form,
    })
  }, [ticker, companyName, form, canSubmit, onFetchEdgar])

  return (
    <form
      onSubmit={handleSubmit}
      className={`
        max-w-xl mx-auto panel p-7
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <PanelHead
        eyebrow="United States · SEC EDGAR"
        title="United States"
        desc="Enter a US-listed ticker. We pull the company's latest annual filing and extract the share-based payment disclosures."
      />

      <GuruFocusLink
        uploading={uploading}
        expectCountry="United States"
        onApply={(g) => onFetchEdgar({
          ticker: (g.ticker || '').toUpperCase(),
          company_name: g.company_name || null,
          form,
        })}
      />

      <div className="grid grid-cols-2 gap-3 mb-3">
        <div>
          <label className="field-label">
            Ticker <span className="text-red-500">*</span>
          </label>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            placeholder="AAPL"
            maxLength={10}
            disabled={uploading}
            className="field-input uppercase"
          />
        </div>
        <div>
          <label className="field-label">
            Form
          </label>
          <select
            value={form}
            onChange={(e) => setForm(e.target.value)}
            disabled={uploading}
            className="field-input"
          >
            <option value="10-K">10-K (annual)</option>
            <option value="10-Q">10-Q (quarterly)</option>
            <option value="20-F">20-F (foreign annual)</option>
          </select>
        </div>
      </div>

      <div className="mb-5">
        <label className="field-label">
          Company name <span className="text-gray-400 font-normal">(optional)</span>
        </label>
        <input
          type="text"
          value={companyName}
          onChange={(e) => setCompanyName(e.target.value)}
          placeholder="Apple Inc."
          disabled={uploading}
          className="field-input"
        />
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="btn btn-primary w-full justify-center"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
        {uploading ? 'Fetching…' : 'Fetch & extract'}
      </button>

      <div className="panel-foot">
        US-listed companies only · Powered by SEC EDGAR
      </div>
    </form>
  )
}

function UkPanel({ onFetchUk, uploading }) {
  const [ticker, setTicker] = useState('')
  const [companyName, setCompanyName] = useState('')

  const canSubmit = (ticker.trim().length > 0 || companyName.trim().length > 0) && !uploading

  const handleSubmit = useCallback((e) => {
    e?.preventDefault?.()
    if (!canSubmit) return
    onFetchUk({
      ticker: ticker.trim().toUpperCase(),
      company_name: companyName.trim() || null,
      category: 'accounts',
    })
  }, [ticker, companyName, canSubmit, onFetchUk])

  return (
    <form
      onSubmit={handleSubmit}
      className={`
        max-w-xl mx-auto panel p-7
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <PanelHead
        eyebrow="United Kingdom · Companies House"
        title="United Kingdom"
        desc="Enter an LSE ticker (e.g. TSCO) or the registered company name. We pull the latest annual accounts and extract automatically."
      />

      <GuruFocusLink
        uploading={uploading}
        expectCountry="United Kingdom"
        onApply={(g) => onFetchUk({
          ticker: (g.ticker || '').toUpperCase(),
          company_name: g.company_name || null,
          category: 'accounts',
        })}
      />

      <div className="grid grid-cols-2 gap-3 mb-3">
        <div>
          <label className="field-label">
            Ticker
          </label>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            placeholder="TSCO"
            maxLength={12}
            disabled={uploading}
            className="field-input uppercase"
          />
        </div>
        <div>
          <label className="field-label">
            Filing
          </label>
          <input
            type="text"
            value="Annual accounts"
            disabled
            className="field-static"
          />
        </div>
      </div>

      <div className="mb-5">
        <label className="field-label">
          Company name <span className="text-gray-400 font-normal">(recommended for accuracy)</span>
        </label>
        <input
          type="text"
          value={companyName}
          onChange={(e) => setCompanyName(e.target.value)}
          placeholder="Tesco PLC"
          disabled={uploading}
          className="field-input"
        />
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="btn btn-primary w-full justify-center"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
        {uploading ? 'Fetching…' : 'Fetch & extract'}
      </button>

      <div className="panel-foot">
        UK-listed companies · Powered by Companies House · Scanned filings are
        OCR'd automatically (may take ~1 min)
      </div>
    </form>
  )
}

function DenmarkPanel({ onFetchDenmark, uploading }) {
  const [ticker, setTicker] = useState('')
  const [companyName, setCompanyName] = useState('')

  const canSubmit = (ticker.trim().length > 0 || companyName.trim().length > 0) && !uploading

  const handleSubmit = useCallback((e) => {
    e?.preventDefault?.()
    if (!canSubmit) return
    onFetchDenmark({
      ticker: ticker.trim().toUpperCase(),
      company_name: companyName.trim() || null,
      category: 'annual',
    })
  }, [ticker, companyName, canSubmit, onFetchDenmark])

  return (
    <form
      onSubmit={handleSubmit}
      className={`
        max-w-xl mx-auto panel p-7
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <PanelHead
        eyebrow="Denmark · Erhvervsstyrelsen"
        title="Denmark"
        desc="Enter a Nasdaq Copenhagen ticker (e.g. NOVO-B) or the registered company name. We pull the latest annual report and extract automatically."
      />

      <GuruFocusLink
        uploading={uploading}
        expectCountry="Denmark"
        onApply={(g) => onFetchDenmark({
          ticker: (g.ticker || '').toUpperCase(),
          company_name: g.company_name || null,
          category: 'annual',
        })}
      />

      <div className="grid grid-cols-2 gap-3 mb-3">
        <div>
          <label className="field-label">
            Ticker
          </label>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            placeholder="NOVO-B"
            maxLength={12}
            disabled={uploading}
            className="field-input uppercase"
          />
        </div>
        <div>
          <label className="field-label">
            Filing
          </label>
          <input
            type="text"
            value="Annual report"
            disabled
            className="field-static"
          />
        </div>
      </div>

      <div className="mb-5">
        <label className="field-label">
          Company name <span className="text-gray-400 font-normal">(recommended for accuracy)</span>
        </label>
        <input
          type="text"
          value={companyName}
          onChange={(e) => setCompanyName(e.target.value)}
          placeholder="Novo Nordisk A/S"
          disabled={uploading}
          className="field-input"
        />
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="btn btn-primary w-full justify-center"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
        {uploading ? 'Fetching…' : 'Fetch & extract'}
      </button>

      <div className="panel-foot">
        Danish-listed companies · Powered by Erhvervsstyrelsen (CVR) · ESEF/iXBRL
        reports are rendered to PDF; scanned filings are OCR'd automatically
      </div>
    </form>
  )
}

function KoreaPanel({ onFetchKorea, uploading }) {
  const [ticker, setTicker] = useState('')
  const [companyName, setCompanyName] = useState('')

  const canSubmit = (ticker.trim().length > 0 || companyName.trim().length > 0) && !uploading

  const handleSubmit = useCallback((e) => {
    e?.preventDefault?.()
    if (!canSubmit) return
    onFetchKorea({
      ticker: ticker.trim().toUpperCase(),
      company_name: companyName.trim() || null,
      category: 'annual',
    })
  }, [ticker, companyName, canSubmit, onFetchKorea])

  return (
    <form
      onSubmit={handleSubmit}
      className={`
        max-w-xl mx-auto panel p-7
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <PanelHead
        eyebrow="South Korea · OpenDART"
        title="South Korea"
        desc="Enter a 6-digit KRX code (e.g. 005930) or the registered company name. We pull the latest annual report (사업보고서) and extract automatically."
      />

      <GuruFocusLink
        uploading={uploading}
        expectCountry="South Korea"
        onApply={(g) => onFetchKorea({
          ticker: (g.ticker || '').toUpperCase(),
          company_name: g.company_name || null,
          category: 'annual',
        })}
      />

      <div className="grid grid-cols-2 gap-3 mb-3">
        <div>
          <label className="field-label">
            Ticker (KRX code)
          </label>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            placeholder="005930"
            maxLength={12}
            disabled={uploading}
            className="field-input uppercase"
          />
        </div>
        <div>
          <label className="field-label">
            Filing
          </label>
          <input
            type="text"
            value="Annual report (사업보고서)"
            disabled
            className="field-static"
          />
        </div>
      </div>

      <div className="mb-5">
        <label className="field-label">
          Company name <span className="text-gray-400 font-normal">(recommended for accuracy)</span>
        </label>
        <input
          type="text"
          value={companyName}
          onChange={(e) => setCompanyName(e.target.value)}
          placeholder="Samsung Electronics"
          disabled={uploading}
          className="field-input"
        />
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="btn btn-primary w-full justify-center"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
        {uploading ? 'Fetching…' : 'Fetch & extract'}
      </button>

      <div className="panel-foot">
        Korea-listed companies · Powered by OpenDART (FSS) · Korean reports are
        detected automatically and results are returned in English
      </div>
    </form>
  )
}

const LEI_RE = /^[A-Z0-9]{18}[0-9]{2}$/
const ISIN_RE = /^[A-Z]{2}[A-Z0-9]{9}[0-9]$/

function EuropePanel({ onFetchEu, onSearchEu, onResolveGuru, uploading }) {
  const [query, setQuery] = useState('')
  const [ticker, setTicker] = useState('')
  const [market, setMarket] = useState('')        // free-text country hint for the scraper
  const [results, setResults] = useState([])
  const [selected, setSelected] = useState(null)   // {lei, name, country}
  const [searching, setSearching] = useState(false)
  const [open, setOpen] = useState(false)
  const [guruMsg, setGuruMsg] = useState(null)     // non-EU / error notice for a GuruFocus link
  const [resolvingGuru, setResolvingGuru] = useState(false)
  const reqId = useRef(0)

  const upper = query.trim().toUpperCase()
  const isLei = LEI_RE.test(upper)
  const isIsin = ISIN_RE.test(upper)
  const isGuru = GURU_RE.test(query.trim())

  // Debounced name search (skipped for raw LEI/ISIN/GuruFocus link, or after a pick).
  useEffect(() => {
    const q = query.trim()
    if (selected && q === selected.name) return
    if (q.length < 2 || isLei || isIsin || isGuru) {
      setResults([]); setOpen(false); return
    }
    setSearching(true)
    const id = ++reqId.current
    const t = setTimeout(async () => {
      const r = await onSearchEu(q)
      if (id !== reqId.current) return     // ignore stale responses
      setResults(r); setOpen(true); setSearching(false)
    }, 300)
    return () => clearTimeout(t)
  }, [query, isLei, isIsin, isGuru, selected, onSearchEu])

  const pick = useCallback((r) => {
    setSelected(r)
    setQuery(r.name)
    setOpen(false)
    setResults([])
  }, [])

  const canSubmit = !uploading && !resolvingGuru &&
    (!!selected || isLei || isIsin || isGuru || query.trim().length >= 2)

  const handleSubmit = useCallback(async (e) => {
    e?.preventDefault?.()
    if (!canSubmit) return
    // ticker + market feed the IR scraper (tried first); the explicit Market
    // dropdown wins over the auto-detected ISO country of an autocomplete pick.
    const tk = ticker.trim().toUpperCase() || undefined

    // GuruFocus link: resolve company name + market from the URL, then run the
    // normal EU flow. Non-European listings are sent to the Diamond tab instead.
    if (isGuru) {
      setGuruMsg(null)
      setResolvingGuru(true)
      try {
        const g = await onResolveGuru(query.trim())
        if (!g) {
          setGuruMsg("Couldn't read that GuruFocus link. Check the URL, or enter the company name instead.")
          return
        }
        if (!g.european) {
          setGuruMsg('This looks like a non-European listing — please use the Diamond tab for it.')
          return
        }
        onFetchEu({
          company_name: g.company_name || undefined,
          ticker: g.ticker || undefined,
          country: g.country || undefined,
          category: 'annual',
        })
      } finally {
        setResolvingGuru(false)
      }
      return
    }

    if (selected) {
      onFetchEu({ lei: selected.lei, company_name: selected.name, ticker: tk,
                  country: market || selected.country, category: 'annual' })
    } else if (isLei) {
      onFetchEu({ lei: upper, ticker: tk, country: market || undefined, category: 'annual' })
    } else if (isIsin) {
      onFetchEu({ isin: upper, ticker: tk, country: market || undefined, category: 'annual' })
    } else {
      onFetchEu({ company_name: query.trim(), ticker: tk,
                  country: market || undefined, category: 'annual' })
    }
  }, [canSubmit, selected, isLei, isIsin, isGuru, upper, query, ticker, market,
      onFetchEu, onResolveGuru])

  return (
    <form
      onSubmit={handleSubmit}
      className={`
        max-w-xl mx-auto panel p-7
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <PanelHead
        eyebrow="EU / EEA · IR scraper + ESEF"
        title="Europe"
        desc="Search a company name and pick it from the list, paste an ISIN or LEI, or paste a GuruFocus stock link. Adding the ticker and market helps us find the company's own annual report first; if that doesn't resolve quickly we fall back to the official ESEF repository."
      />

      <div className="relative mb-1">
        <label className="field-label">
          Company name, ISIN, LEI, or GuruFocus link <span className="text-red-500">*</span>
        </label>
        <div className="relative">
          <Search className="w-4 h-4 text-gray-400 absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            value={query}
            onChange={(e) => { setQuery(e.target.value); setSelected(null); setGuruMsg(null) }}
            onFocus={() => results.length && setOpen(true)}
            placeholder="e.g. Hermès · ASML · NL0010273215 · gurufocus.com/stock/OSL:AUSS/summary"
            disabled={uploading}
            autoComplete="off"
            className="w-full pl-9 pr-9 py-2 text-sm rounded-md border border-gray-300 bg-white focus:outline-none focus:ring-2 focus:ring-brand focus:border-brand"
          />
          {searching && (
            <Loader2 className="w-4 h-4 text-gray-400 animate-spin absolute right-3 top-1/2 -translate-y-1/2" />
          )}
        </div>

        {/* Autocomplete dropdown */}
        {open && results.length > 0 && (
          <ul className="absolute z-10 mt-1 w-full bg-white border border-gray-200 rounded-md shadow-lg max-h-64 overflow-auto">
            {results.map((r) => (
              <li key={r.lei}>
                <button
                  type="button"
                  onClick={() => pick(r)}
                  className="w-full flex items-center justify-between gap-2 px-3 py-2 text-left hover:bg-brand-pale"
                >
                  <span className="text-sm text-gray-900 truncate">{r.name}</span>
                  <span className="text-[11px] font-medium text-gray-500 bg-gray-100 rounded px-1.5 py-0.5 flex-shrink-0">
                    {r.country || '—'}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Selection / detection hint */}
      <div className="min-h-[20px] mb-4 text-xs">
        {guruMsg ? (
          <span className="text-amber-700 flex items-center gap-1">
            <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
            {guruMsg}
          </span>
        ) : isGuru ? (
          <span className="text-gray-500">
            GuruFocus link detected — we'll auto-detect the company &amp; market and fetch its annual report.
          </span>
        ) : selected ? (
          <span className="text-green-700 flex items-center gap-1">
            <Check className="w-3.5 h-3.5" />
            {selected.name} · country auto-detected: <strong>{selected.country || '—'}</strong>
          </span>
        ) : isLei ? (
          <span className="text-gray-500">LEI entered — will fetch directly.</span>
        ) : isIsin ? (
          <span className="text-gray-500">ISIN entered — will resolve via GLEIF.</span>
        ) : query.trim().length >= 2 && !open ? (
          <span className="text-gray-400">Type to search, then pick a company.</span>
        ) : null}
      </div>

      {/* Ticker + Market — optional hints that improve the IR scraper (tried first). */}
      <div className="grid grid-cols-2 gap-3 mb-4">
        <div>
          <label className="field-label">
            Ticker <span className="text-gray-400 font-normal">(optional)</span>
          </label>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            placeholder="e.g. ASML · MC"
            maxLength={12}
            disabled={uploading}
            autoComplete="off"
            className="field-input uppercase"
          />
        </div>
        <div>
          <label className="field-label">
            Market / Country <span className="text-gray-400 font-normal">(optional)</span>
          </label>
          <CountrySelect
            options={EU_MARKETS}
            value={market}
            onChange={setMarket}
            placeholder="Auto-detect"
            disabled={uploading}
          />
        </div>
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="btn btn-primary w-full justify-center"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
        {uploading ? 'Fetching…' : 'Fetch & extract'}
      </button>

      <div className="panel-foot">
        Tries the company's own investor-relations site first, then falls back to
        the official ESEF repository (filings.xbrl.org) after 100 seconds.
      </div>
    </form>
  )
}

function CanadaPanel({ onFetchCanada, onUpload, uploading }) {
  const [ticker, setTicker] = useState('')
  const [companyName, setCompanyName] = useState('')
  const [showUpload, setShowUpload] = useState(false)

  const canSubmit = ticker.trim().length > 0 && !uploading

  const handleSubmit = useCallback((e) => {
    e?.preventDefault?.()
    if (!canSubmit) return
    onFetchCanada({
      ticker: ticker.trim().toUpperCase(),
      company_name: companyName.trim() || null,
      category: 'annual',
    })
  }, [ticker, companyName, canSubmit, onFetchCanada])

  return (
    <form
      onSubmit={handleSubmit}
      className={`
        max-w-xl mx-auto panel p-7
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <PanelHead
        eyebrow="Canada · SEC MJDS"
        title="Canada"
        desc="Enter the ticker of a US-cross-listed Canadian company (e.g. SHOP, BAM, CNQ). We pull its SEC MJDS annual report (40-F) and extract automatically."
      />

      <GuruFocusLink
        uploading={uploading}
        expectCountry="Canada"
        onApply={(g) => onFetchCanada({
          ticker: (g.ticker || '').toUpperCase(),
          company_name: g.company_name || null,
          category: 'annual',
        })}
      />

      <div className="grid grid-cols-2 gap-3 mb-3">
        <div>
          <label className="field-label">
            Ticker <span className="text-red-500">*</span>
          </label>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            placeholder="SHOP"
            maxLength={12}
            disabled={uploading}
            className="field-input uppercase"
          />
        </div>
        <div>
          <label className="field-label">
            Filing
          </label>
          <input
            type="text"
            value="Annual report (40-F / 20-F)"
            disabled
            className="field-static"
          />
        </div>
      </div>

      <div className="mb-5">
        <label className="field-label">
          Company name <span className="text-gray-400 font-normal">(optional)</span>
        </label>
        <input
          type="text"
          value={companyName}
          onChange={(e) => setCompanyName(e.target.value)}
          placeholder="Shopify Inc."
          disabled={uploading}
          className="field-input"
        />
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="btn btn-primary w-full justify-center"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
        {uploading ? 'Fetching…' : 'Fetch & extract'}
      </button>

      <div className="panel-foot">
        Cross-listed (SEC-registered) Canadian issuers · via SEC EDGAR MJDS.<br />
        SEDAR+ has no open API (bot-walled), so TSX-only issuers aren't fetchable —
        {' '}
        <button
          type="button"
          onClick={() => setShowUpload((v) => !v)}
          className="text-brand underline"
        >
          upload their PDF instead
        </button>.
      </div>

      {showUpload && (
        <div className="mt-4 pt-4 border-t border-gray-200">
          <UploadPanel onUpload={onUpload} uploading={uploading} />
        </div>
      )}
    </form>
  )
}

function GermanyPanel({ onFetchGermany, onUpload, uploading }) {
  const [companyName, setCompanyName] = useState('')
  const [ticker, setTicker] = useState('')
  const [showUpload, setShowUpload] = useState(false)

  const canSubmit = (companyName.trim().length > 0 || ticker.trim().length > 0) && !uploading

  const handleSubmit = useCallback((e) => {
    e?.preventDefault?.()
    if (!canSubmit) return
    onFetchGermany({
      ticker: ticker.trim().toUpperCase() || undefined,
      company_name: companyName.trim() || null,
      category: 'annual',
    })
  }, [companyName, ticker, canSubmit, onFetchGermany])

  return (
    <form
      onSubmit={handleSubmit}
      className={`
        max-w-xl mx-auto panel p-7
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <PanelHead
        eyebrow="Germany · IR site + SEC EDGAR"
        title="Germany"
        desc="Enter the company name (and ticker, if you have it). We search the company's own investor-relations site for the latest annual report first, then fall back to SEC EDGAR (German blue-chips like SAP file a 20-F)."
      />

      <GuruFocusLink
        uploading={uploading}
        expectCountry="Germany"
        onApply={(g) => onFetchGermany({
          ticker: (g.ticker || '').toUpperCase() || undefined,
          company_name: g.company_name || null,
          category: 'annual',
        })}
      />

      <div className="mb-3">
        <label className="field-label">
          Company name <span className="text-gray-400 font-normal">(recommended)</span>
        </label>
        <input
          type="text"
          value={companyName}
          onChange={(e) => setCompanyName(e.target.value)}
          placeholder="e.g. SAP SE · Siemens AG · Allianz SE"
          disabled={uploading}
          className="field-input"
        />
      </div>

      <div className="mb-5">
        <label className="field-label">
          Ticker <span className="text-gray-400 font-normal">(optional)</span>
        </label>
        <input
          type="text"
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="SAP"
          maxLength={12}
          disabled={uploading}
          className="field-input uppercase"
        />
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="btn btn-primary w-full justify-center"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
        {uploading ? 'Fetching…' : 'Fetch & extract'}
      </button>

      <div className="panel-foot">
        Germany's Bundesanzeiger has no open API, so we use the company's IR site
        (then SEC EDGAR). If neither has a downloadable report,{' '}
        <button
          type="button"
          onClick={() => setShowUpload((v) => !v)}
          className="text-brand underline"
        >
          upload the PDF instead
        </button>.
      </div>

      {showUpload && (
        <div className="mt-4 pt-4 border-t border-gray-200">
          <UploadPanel onUpload={onUpload} uploading={uploading} />
        </div>
      )}
    </form>
  )
}

function SourcePanel({ cfg, onFetchSimple, uploading }) {
  const [ticker, setTicker] = useState('')
  const [companyName, setCompanyName] = useState('')

  const canSubmit = (cfg.requireTicker
    ? ticker.trim().length > 0
    : (ticker.trim().length > 0 || companyName.trim().length > 0)) && !uploading

  const handleSubmit = useCallback((e) => {
    e?.preventDefault?.()
    if (!canSubmit) return
    onFetchSimple(cfg.endpoint, cfg.errLabel, {
      ticker: ticker.trim().toUpperCase(),
      company_name: companyName.trim() || null,
      category: 'annual',
    })
  }, [ticker, companyName, canSubmit, cfg, onFetchSimple])

  return (
    <form
      onSubmit={handleSubmit}
      className={`
        max-w-xl mx-auto panel p-7
        ${uploading ? 'opacity-60 pointer-events-none' : ''}
      `}
    >
      <PanelHead
        eyebrow={cfg.errLabel.replace(/\s*\((.+)\)$/, ' · $1')}
        title={cfg.title}
        desc={cfg.desc}
      />

      <GuruFocusLink
        uploading={uploading}
        onApply={(g) => onFetchSimple(cfg.endpoint, cfg.errLabel, {
          ticker: (g.ticker || '').toUpperCase(),
          company_name: g.company_name || null,
          category: 'annual',
        })}
      />

      <div className="grid grid-cols-2 gap-3 mb-3">
        <div>
          <label className="field-label">
            {cfg.tickerLabel}{cfg.requireTicker && <span className="text-red-500"> *</span>}
          </label>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            placeholder={cfg.placeholder}
            maxLength={14}
            disabled={uploading}
            className="field-input uppercase"
          />
        </div>
        <div>
          <label className="field-label">
            Filing
          </label>
          <input
            type="text"
            value={cfg.filing}
            disabled
            className="field-static"
          />
        </div>
      </div>

      <div className="mb-5">
        <label className="field-label">
          Company name{' '}
          <span className="text-gray-400 font-normal">
            {cfg.requireTicker ? '(optional)' : '(recommended for accuracy)'}
          </span>
        </label>
        <input
          type="text"
          value={companyName}
          onChange={(e) => setCompanyName(e.target.value)}
          placeholder="Company Inc."
          disabled={uploading}
          className="field-input"
        />
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="btn btn-primary w-full justify-center"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
        {uploading ? 'Fetching…' : 'Fetch & extract'}
      </button>

      <div className="panel-foot">
        {cfg.footer}
      </div>
    </form>
  )
}

// Shared left-aligned header used by every source panel: small gold eyebrow,
// serif title, one-line description — the research-report motif.
function PanelHead({ eyebrow, title, desc }) {
  return (
    <div className="panel-head">
      {eyebrow && <div className="eyebrow mb-1.5">{eyebrow}</div>}
      <h2 className="panel-title">{title}</h2>
      {desc && <p className="panel-desc">{desc}</p>}
    </div>
  )
}
