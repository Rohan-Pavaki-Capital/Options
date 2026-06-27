import { useState, useEffect, useCallback } from 'react'
import { BarChart3, ChevronRight } from 'lucide-react'
import UploadScreen from './components/UploadScreen'
import ProcessingScreen from './components/ProcessingScreen'
import ResultsScreen from './components/ResultsScreen'
import TestingScreen from './components/TestingScreen'
import ResultModal from './components/ResultModal'
import Sidebar from './components/Sidebar'
import { COUNTRY_META } from './components/markets'
import { friendlyError } from './errorCopy'

// Friendly headers for the non-country tools shown in the main work area.
const SPECIAL_META = {
  diamond: { label: 'Search any company', kicker: 'Any market · auto-find' },
  upload:  { label: 'Upload a report', kicker: 'Manual PDF' },
  testing: { label: 'Testing', kicker: 'Diagnostic · scraper only' },
}

const API_BASE = `${import.meta.env.VITE_API_BASE || ''}/api`

export default function App() {
  // State machine: 'idle' | 'uploading' | 'processing' | 'completed' | 'failed'
  const [screen, setScreen] = useState('idle')
  const [mode, setMode] = useState('diamond')   // active source key (sidebar selection)
  const [jobId, setJobId] = useState(null)
  const [jobStatus, setJobStatus] = useState(null)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)        // friendly inline banner text
  const [modal, setModal] = useState(null)        // friendly centered popup { title, body }

  // Turn any failure (a tagged job error OR a raw HTTP/exception string) into a
  // friendly, non-technical message and show it as a popup or inline banner.
  const presentError = useCallback((raw, code, context, alt) => {
    const f = friendlyError({ code, context, raw })
    if (f.variant === 'modal') {
      // For "no options data" (NO_PAGES) a valid source PDF was still fetched —
      // offer a link to view it. Other modal outcomes have no usable PDF.
      const pdfUrl = (f.code === 'NO_PAGES' && jobId)
        ? `${API_BASE}/download/${jobId}/pdf`
        : null
      // EU tab: when the annual report had no data but the scraper also saved a
      // recent interim/quarterly, offer to run it instead.
      let altLabel = null, altJobId = null
      if (f.code === 'NO_PAGES' && alt?.available && jobId) {
        const yr = alt.year ? ` (FY${alt.year})` : ''
        const kind = alt.kind === 'interim' ? 'quarterly / interim report' : `${alt.kind} report`
        altLabel = `${kind}${yr}`
        altJobId = jobId
      }
      setModal({ title: f.title, body: f.body, pdfUrl, altLabel, altJobId })
      setError(null)
    } else {
      setError(f.body)
      setModal(null)
    }
    setScreen('failed')
  }, [jobId])

  const handleTryAlternate = useCallback(async (failedJobId) => {
    if (!failedJobId) return
    setModal(null)
    setError(null)
    setScreen('uploading')
    try {
      const res = await fetch(`${API_BASE}/eu-try-alternate/${failedJobId}`, { method: 'POST' })
      if (!res.ok) {
        const e = await res.json().catch(() => ({}))
        throw new Error(e.detail || `Could not start the interim report (${res.status})`)
      }
      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [presentError])

  // Poll job status every 1s while processing
  useEffect(() => {
    if (!jobId || screen !== 'processing') return

    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/job/${jobId}`)
        if (!res.ok) throw new Error('Failed to fetch job status')
        const data = await res.json()
        setJobStatus(data)

        if (data.status === 'completed') {
          // Fetch the final result
          const resultRes = await fetch(`${API_BASE}/result/${jobId}`)
          if (resultRes.ok) {
            const resultData = await resultRes.json()
            setResult(resultData)
            setScreen('completed')
          }
        } else if (data.status === 'failed') {
          presentError(data.error, data.error_code, data.error_context, {
            available: data.alt_report_available,
            kind: data.alt_report_kind,
            year: data.alt_report_year,
          })
        }
      } catch (err) {
        console.error('Polling error:', err)
      }
    }, 1000)

    return () => clearInterval(interval)
  }, [jobId, screen])

  const handleUpload = useCallback(async (file) => {
    setError(null)
    setScreen('uploading')

    const formData = new FormData()
    formData.append('file', file)

    try {
      const res = await fetch(`${API_BASE}/extract`, {
        method: 'POST',
        body: formData,
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || `Upload failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [])

  const handleFetchEdgar = useCallback(async ({ ticker, company_name, form }) => {
    setError(null)
    setScreen('uploading')

    try {
      const res = await fetch(`${API_BASE}/extract-from-edgar`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker, company_name, form }),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || `EDGAR fetch failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [])

  const handleFetchUk = useCallback(async ({ ticker, company_name, category }) => {
    setError(null)
    setScreen('uploading')

    try {
      const res = await fetch(`${API_BASE}/extract-from-uk`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker, company_name, category }),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || `Companies House fetch failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [])

  const handleFetchDenmark = useCallback(async ({ ticker, company_name, category }) => {
    setError(null)
    setScreen('uploading')

    try {
      const res = await fetch(`${API_BASE}/extract-from-denmark`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker, company_name, category }),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || `Denmark (CVR) fetch failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [])

  const handleFetchKorea = useCallback(async ({ ticker, company_name, category }) => {
    setError(null)
    setScreen('uploading')

    try {
      const res = await fetch(`${API_BASE}/extract-from-korea`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker, company_name, category }),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || `Korea (DART) fetch failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [])

  const handleSearchEu = useCallback(async (q) => {
    const query = (q || '').trim()
    if (query.length < 2) return []
    try {
      const res = await fetch(`${API_BASE}/eu-search?q=${encodeURIComponent(query)}&limit=8`)
      if (!res.ok) return []
      const data = await res.json()
      return data.results || []
    } catch (err) {
      console.error('EU search error:', err)
      return []
    }
  }, [])

  // Resolve a GuruFocus stock URL -> {company_name, ticker, exchange, country, european}.
  // Used by the EU tab to auto-fill the company + market from a pasted link.
  const handleResolveGuru = useCallback(async (url) => {
    try {
      const res = await fetch(`${API_BASE}/gurufocus-resolve?url=${encodeURIComponent(url)}`)
      if (!res.ok) return null
      return await res.json()
    } catch (err) {
      console.error('GuruFocus resolve error:', err)
      return null
    }
  }, [])

  const handleFetchEu = useCallback(async ({ lei, isin, ticker, company_name, country, category }) => {
    setError(null)
    setScreen('uploading')

    try {
      const res = await fetch(`${API_BASE}/extract-from-eu`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lei, isin, ticker, company_name, country, category }),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || `EU (ESEF) fetch failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [])

  const handleFetchCanada = useCallback(async ({ ticker, company_name, category }) => {
    setError(null)
    setScreen('uploading')

    try {
      const res = await fetch(`${API_BASE}/extract-from-canada`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker, company_name, category }),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || `Canada (SEC MJDS) fetch failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [])

  const handleFetchGermany = useCallback(async ({ ticker, company_name, category }) => {
    setError(null)
    setScreen('uploading')

    try {
      const res = await fetch(`${API_BASE}/extract-from-germany`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker, company_name, category }),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || `Germany fetch failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [])

  // Generic ticker-based fetch for the simple markets (Japan, Brazil, Taiwan,
  // China, India, Hong Kong, Indonesia) — they all share an identical flow,
  // differing only by endpoint. The dedicated handlers above stay for markets
  // with bespoke UIs (EDGAR form, EU autocomplete, Canada upload fallback).
  const handleFetchSimple = useCallback(async (endpoint, label, payload) => {
    setError(null)
    setScreen('uploading')

    try {
      const res = await fetch(`${API_BASE}/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || `${label} fetch failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [])

  const handleFetchDiamond = useCallback(async ({ company_name, ticker, country }) => {
    setError(null)
    setScreen('uploading')

    try {
      const res = await fetch(`${API_BASE}/extract-from-diamond`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company_name, ticker, country }),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        let detail = errData.detail
        if (Array.isArray(detail)) {
          // FastAPI validation errors come as a list of {loc,msg,...} objects.
          detail = detail.map((d) => d?.msg || JSON.stringify(d)).join('; ')
        } else if (detail && typeof detail === 'object') {
          detail = JSON.stringify(detail)
        }
        throw new Error(detail || `Diamond fetch failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [])

  const handleFetchSingapore = useCallback(async ({ company_name, ticker }) => {
    setError(null)
    setScreen('uploading')

    try {
      const res = await fetch(`${API_BASE}/extract-from-singapore`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company_name, ticker }),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        let detail = errData.detail
        if (Array.isArray(detail)) {
          detail = detail.map((d) => d?.msg || JSON.stringify(d)).join('; ')
        } else if (detail && typeof detail === 'object') {
          detail = JSON.stringify(detail)
        }
        throw new Error(detail || `Singapore fetch failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [])

  const handleFetchMexico = useCallback(async ({ company_name, ticker }) => {
    setError(null)
    setScreen('uploading')

    try {
      const res = await fetch(`${API_BASE}/extract-from-mexico`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company_name, ticker }),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        let detail = errData.detail
        if (Array.isArray(detail)) {
          detail = detail.map((d) => d?.msg || JSON.stringify(d)).join('; ')
        } else if (detail && typeof detail === 'object') {
          detail = JSON.stringify(detail)
        }
        throw new Error(detail || `Mexico fetch failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [presentError])

  const handleFetchTest = useCallback(async ({ company_name, ticker, country }) => {
    setError(null)
    setScreen('uploading')

    try {
      const res = await fetch(`${API_BASE}/scrape-test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company_name, ticker, country }),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || `Scrape test failed (${res.status})`)
      }

      const data = await res.json()
      setJobId(data.job_id)
      setScreen('processing')
    } catch (err) {
      presentError(err.message)
    }
  }, [])

  const handleReset = useCallback(() => {
    setScreen('idle')
    setJobId(null)
    setJobStatus(null)
    setResult(null)
    setError(null)
  }, [])

  // Sidebar selection: switch the active source and return to a clean form.
  const handleSelectMode = useCallback((m) => {
    setMode(m)
    setScreen('idle')
    setJobId(null)
    setJobStatus(null)
    setResult(null)
    setError(null)
  }, [])

  const handleCancel = useCallback(async () => {
    if (!jobId) return
    try {
      await fetch(`${API_BASE}/job/${jobId}`, { method: 'DELETE' })
    } catch (err) {
      console.error('Cancel failed:', err)
    }
    handleReset()
  }, [jobId, handleReset])

  // Label shown in the breadcrumb for the active source (country or tool).
  const activeMeta = COUNTRY_META[mode]
  const activeLabel = activeMeta ? activeMeta.label : (SPECIAL_META[mode]?.label || 'Extraction')
  const activeKicker = activeMeta ? activeMeta.region : (SPECIAL_META[mode]?.kicker || '')

  return (
    <div className="min-h-screen bg-canvas flex flex-col">
      {/* App Header — institutional navy bar */}
      <header className="bg-brand border-b border-navy-900 sticky top-0 z-20">
        <div className="max-w-[1400px] mx-auto px-6 h-[52px] flex items-center justify-between">
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-8 h-8 rounded-md bg-white/[0.06] ring-1 ring-white/10 flex items-center justify-center flex-shrink-0">
              <BarChart3 className="w-[18px] h-[18px] text-accent-light" />
            </div>
            <div className="flex items-baseline gap-3 min-w-0">
              <span className="font-serif font-semibold text-[17px] text-white leading-none tracking-tight">
                Pavaki
              </span>
              <span className="hidden sm:block w-px h-3.5 bg-white/20 self-center" />
              <span className="hidden sm:block text-[11px] font-medium uppercase tracking-[0.14em] text-white/60 leading-none">
                Options Extractor
              </span>
            </div>
          </div>

          <div className="flex items-center gap-3">
            {screen !== 'idle' && (
              <button
                onClick={handleReset}
                className="text-xs font-medium px-3 py-1.5 rounded-md border border-white/15 text-white/85 hover:bg-white/10 hover:border-white/25 transition-colors"
              >
                New extraction
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Console body — left sidebar + main work area */}
      <div className="flex-1 w-full max-w-[1400px] mx-auto flex flex-col lg:flex-row">
        <Sidebar
          mode={mode}
          onSelect={handleSelectMode}
          disabled={screen === 'uploading'}
        />

        <main className="flex-1 min-w-0 px-6 py-8">
          {(screen === 'idle' || screen === 'uploading' || screen === 'failed') && (
            <div className="animate-enter">
              {/* Breadcrumb — orients the user within the console */}
              <div className="flex items-center flex-wrap gap-2 text-xs mb-7 pb-4 border-b border-hairline">
                <span className="font-medium text-gray-400">New extraction</span>
                <ChevronRight className="w-3.5 h-3.5 text-gray-300" />
                <span className="font-semibold text-ink">{activeLabel}</span>
                {activeKicker && (
                  <span className="text-gray-400">· {activeKicker}</span>
                )}
              </div>

              <UploadScreen
                mode={mode}
                onUpload={handleUpload}
                onFetchDiamond={handleFetchDiamond}
                onFetchEdgar={handleFetchEdgar}
                onFetchUk={handleFetchUk}
                onFetchDenmark={handleFetchDenmark}
                onFetchKorea={handleFetchKorea}
                onFetchEu={handleFetchEu}
                onSearchEu={handleSearchEu}
                onResolveGuru={handleResolveGuru}
                onFetchCanada={handleFetchCanada}
                onFetchGermany={handleFetchGermany}
                onFetchSingapore={handleFetchSingapore}
                onFetchMexico={handleFetchMexico}
                onFetchSimple={handleFetchSimple}
                onFetchTest={handleFetchTest}
                uploading={screen === 'uploading'}
                error={error}
              />
            </div>
          )}

          {screen === 'processing' && (
            <div className="animate-enter">
              {jobStatus?.source === 'scrape_test' ? (
                <TestingScreen
                  jobStatus={jobStatus}
                  jobId={jobId}
                  apiBase={API_BASE}
                  onCancel={handleCancel}
                />
              ) : (
                <ProcessingScreen
                  jobStatus={jobStatus}
                  onCancel={handleCancel}
                />
              )}
            </div>
          )}

          {screen === 'completed' && result && (
            <div className="animate-enter">
              {result?.mode === 'scrape_test' ? (
                <TestingScreen
                  result={result}
                  jobId={jobId}
                  apiBase={API_BASE}
                  onReset={handleReset}
                />
              ) : (
                <ResultsScreen
                  result={result}
                  jobId={jobId}
                  apiBase={API_BASE}
                  onReset={handleReset}
                />
              )}
            </div>
          )}
        </main>
      </div>

      <footer className="border-t border-hairline bg-paper">
        <div className="max-w-[1400px] mx-auto px-6 py-5 flex items-center justify-between gap-3 text-[11px] text-gray-400 tracking-wide flex-wrap">
          <span className="font-serif text-gray-500">Pavaki · Options Extractor</span>
          <span>Share-based payment disclosures, structured to Excel</span>
        </div>
      </footer>

      <ResultModal
        open={screen === 'failed' && !!modal}
        title={modal?.title}
        body={modal?.body}
        pdfUrl={modal?.pdfUrl}
        altLabel={modal?.altLabel}
        onAlt={modal?.altJobId ? () => handleTryAlternate(modal.altJobId) : null}
        onClose={() => setModal(null)}
      />
    </div>
  )
}
