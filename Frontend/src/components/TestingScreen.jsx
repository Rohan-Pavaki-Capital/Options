import {
  FlaskConical, Loader2, FileText, Download, Clock, Coins,
  Globe, RotateCcw,
} from 'lucide-react'

// Single component for the TESTING flow. Shows a lightweight "fetching" view
// while the scrape runs, then a metrics + PDF-download view when done.
export default function TestingScreen({
  jobStatus, result, jobId, apiBase, onReset, onCancel,
}) {
  const done = !!result

  if (!done) {
    const elapsed = jobStatus?.elapsed_seconds
    const stage = jobStatus?.stages?.scrape_fetch
    return (
      <div className="card p-12 text-center max-w-2xl mx-auto">
        <div className="w-11 h-11 mx-auto mb-4 rounded-full bg-accent-pale border border-accent/30 flex items-center justify-center">
          <Loader2 className="w-5 h-5 text-accent-dark animate-spin" />
        </div>
        <div className="text-[17px] font-serif text-ink mb-1.5">
          Fetching the latest filing…
        </div>
        <div className="text-[13px] text-gray-500 mb-4 leading-relaxed">
          Source test — fetch only. {stage?.details || 'Resolving the source and downloading the PDF.'}
        </div>
        <div className="text-[11.5px] text-gray-400 mb-6 tnum">
          Elapsed {elapsed != null ? `${elapsed.toFixed(1)}s` : '…'}
        </div>
        <button onClick={onCancel} className="btn text-xs">Cancel</button>
      </div>
    )
  }

  const fc = result.firecrawl || {}
  const sizeMb = result.pdf_size ? (result.pdf_size / 1024 / 1024).toFixed(2) : '0'

  return (
    <div className="card overflow-hidden max-w-2xl mx-auto">
      {/* Header */}
      <div className="px-6 py-4 border-b border-hairline bg-accent-pale/50 flex items-center gap-3">
        <div className="w-10 h-10 rounded-lg bg-paper border border-accent/30 flex items-center justify-center">
          <FlaskConical className="w-5 h-5 text-accent-dark" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-lg font-serif text-ink truncate leading-tight">
            {result.company || result.ticker || 'Filing'}
          </div>
          <div className="text-xs text-gray-500">
            Scraper test · no extraction
          </div>
        </div>
        <span className="text-xs px-3 py-1 bg-emerald-50 text-emerald-700 rounded-md font-semibold ring-1 ring-emerald-200">
          PDF ready
        </span>
      </div>

      <div className="p-6">
        {/* Metric tiles */}
        <div className="grid grid-cols-3 gap-3 mb-5">
          <Metric
            icon={<Coins className="w-4 h-4 text-accent-dark" />}
            label="Firecrawl credits"
            value={`~${fc.credits_derived ?? 0}`}
            sub={`${fc.scrapes ?? 0} scrape(s)`}
          />
          <Metric
            icon={<Coins className="w-4 h-4 text-accent-dark" />}
            label="Ledger Δ (billed)"
            value={fc.ledger_delta != null ? `${fc.ledger_delta}` : 'n/a'}
            sub={fc.ledger_delta != null ? 'from live balance' : 'endpoint unavailable'}
          />
          <Metric
            icon={<Clock className="w-4 h-4 text-accent-dark" />}
            label="Time taken"
            value={`${(result.elapsed_seconds ?? 0).toFixed(1)}s`}
            sub="fetch only"
          />
        </div>

        {/* Filing details */}
        <div className="rounded-lg border border-gray-200 divide-y divide-gray-100 mb-5">
          <DetailRow icon={<Globe className="w-4 h-4 text-gray-400" />} label="Source"
            value={result.diamond_source || '—'} />
          <DetailRow icon={<FileText className="w-4 h-4 text-gray-400" />} label="Form"
            value={result.form || '—'} />
          <DetailRow icon={<FileText className="w-4 h-4 text-gray-400" />} label="Report period"
            value={result.report_period || '—'} />
          <DetailRow icon={<FileText className="w-4 h-4 text-gray-400" />} label="PDF"
            value={`${result.pdf_filename || 'filing.pdf'} · ${sizeMb} MB`} />
        </div>

        {/* Actions */}
        <div className="flex items-center gap-3">
          <a
            href={`${apiBase}/download/${jobId}/pdf`}
            className="btn btn-primary flex-1 justify-center"
          >
            <Download className="w-4 h-4" />
            Download PDF
          </a>
          <button onClick={onReset} className="btn justify-center">
            <RotateCcw className="w-4 h-4" />
            Run another
          </button>
        </div>

        {result.url && (
          <div className="text-xs text-gray-400 mt-4 break-all">
            Source URL: {result.url}
          </div>
        )}
      </div>
    </div>
  )
}

function Metric({ icon, label, value, sub }) {
  return (
    <div className="p-3 metric-card">
      <div className="flex items-center gap-1.5 mb-1">{icon}
        <span className="text-[11px] font-medium text-gray-500">{label}</span>
      </div>
      <div className="text-lg font-serif text-ink tnum">{value}</div>
      <div className="text-[11px] text-gray-400 mt-0.5">{sub}</div>
    </div>
  )
}

function DetailRow({ icon, label, value }) {
  return (
    <div className="flex items-center gap-3 px-3.5 py-2.5">
      {icon}
      <span className="text-xs text-gray-500 w-28 shrink-0">{label}</span>
      <span className="text-sm text-gray-900 truncate">{value}</span>
    </div>
  )
}
