import { Check, Loader2, FileText, ShieldCheck, FileSpreadsheet, Search, Brain, Download } from 'lucide-react'

const BASE_STAGES = [
  { key: 'stage1_keywords', label: 'Locate disclosure pages', icon: Search },
  { key: 'stage2_classifier', label: 'Confirm relevant pages', icon: Brain },
  { key: 'stage3_extraction', label: 'Extract plan data', icon: FileText },
  { key: 'validation', label: 'Validate & cross-check figures', icon: ShieldCheck },
  { key: 'excel_generation', label: 'Build Excel workbook', icon: FileSpreadsheet },
]

const EDGAR_FETCH_STAGE = {
  key: 'edgar_fetch',
  label: 'Fetch filing from SEC EDGAR',
  icon: Download,
}

const CH_FETCH_STAGE = {
  key: 'ch_fetch',
  label: 'Fetch & OCR filing from Companies House',
  icon: Download,
}

export default function ProcessingScreen({ jobStatus, onCancel }) {
  if (!jobStatus) {
    return (
      <div className="card p-12 text-center">
        <Loader2 className="w-7 h-7 text-brand animate-spin mx-auto mb-3" />
        <div className="text-sm text-gray-500">Preparing extraction…</div>
      </div>
    )
  }

  const { filename, file_size, progress, current_stage, stages, elapsed_seconds, estimated_remaining } = jobStatus

  return (
    <div className="card overflow-hidden max-w-2xl mx-auto">
      {/* Header */}
      <div className="px-6 py-4 border-b border-hairline bg-canvas flex items-center gap-3">
        <div className="w-9 h-9 rounded-md bg-paper border border-hairline flex items-center justify-center flex-shrink-0">
          <FileText className="w-4 h-4 text-brand/70" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-sm font-semibold text-ink truncate">{filename}</div>
          <div className="text-[11.5px] text-gray-500 mt-px">
            {(file_size / 1024 / 1024).toFixed(1)} MB · started {new Date(jobStatus.created_at).toLocaleTimeString()}
          </div>
        </div>
        <span className="text-[11px] px-2.5 py-1 bg-brand/5 text-brand rounded font-semibold flex items-center gap-1.5 ring-1 ring-brand/10 uppercase tracking-wide">
          <Loader2 className="w-3 h-3 animate-spin" />
          Processing
        </span>
      </div>

      <div className="p-6">
        {/* Progress bar */}
        <div className="mb-6">
          <div className="flex justify-between items-baseline mb-2">
            <span className="eyebrow">Overall progress</span>
            <span className="text-sm font-semibold text-ink tnum">{progress}%</span>
          </div>
          <div className="h-[7px] bg-gray-100 rounded-full overflow-hidden">
            <div
              className="h-full bg-brand rounded-full transition-all duration-500 progress-active"
              style={{ width: `${progress}%` }}
            />
          </div>
        </div>

        {/* Stage timeline */}
        <div>
          {(stages.edgar_fetch
              ? [EDGAR_FETCH_STAGE, ...BASE_STAGES]
              : stages.ch_fetch
                ? [CH_FETCH_STAGE, ...BASE_STAGES]
                : BASE_STAGES
          ).map((stage, idx, arr) => {
            const stageData = stages[stage.key] || { status: 'pending' }
            return (
              <StageRow
                key={stage.key}
                label={stage.label}
                stageData={stageData}
                isCurrent={current_stage === stage.key}
                isLast={idx === arr.length - 1}
              />
            )
          })}
        </div>

        {/* Footer stats */}
        <div className="mt-5 pt-4 border-t border-hairline flex gap-6 text-xs flex-wrap items-center">
          <Stat label="Elapsed" value={`${elapsed_seconds?.toFixed(1)}s`} />
          {estimated_remaining != null && (
            <Stat label="Est. remaining" value={`~${estimated_remaining.toFixed(0)}s`} />
          )}
          <button onClick={onCancel} className="btn btn-ghost ml-auto text-xs h-8">
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}

function StageRow({ label, stageData, isCurrent, isLast }) {
  const status = stageData.status
  const done = status === 'completed'

  return (
    <div className="flex gap-3.5">
      {/* Rail: node + connector */}
      <div className="flex flex-col items-center">
        <div
          className={`
            w-[26px] h-[26px] rounded-full flex items-center justify-center flex-shrink-0 border
            ${done
              ? 'bg-brand border-brand'
              : isCurrent
                ? 'bg-paper border-brand/50'
                : 'bg-paper border-hairline'}
          `}
        >
          {done ? (
            <Check className="w-3.5 h-3.5 text-white" strokeWidth={3} />
          ) : isCurrent ? (
            <Loader2 className="w-3.5 h-3.5 text-brand animate-spin" />
          ) : (
            <span className="w-1.5 h-1.5 rounded-full bg-gray-300" />
          )}
        </div>
        {!isLast && (
          <div className={`w-px flex-1 my-1 ${done ? 'bg-brand/40' : 'bg-hairline'}`} />
        )}
      </div>

      {/* Body */}
      <div className={`flex-1 min-w-0 pb-5 ${!done && !isCurrent ? 'opacity-45' : ''}`}>
        <div className="flex justify-between items-baseline gap-3 pt-[3px]">
          <span className={`text-[13.5px] leading-snug ${isCurrent ? 'font-semibold text-brand' : 'font-medium text-ink'}`}>
            {label}
          </span>
          <span className={`text-[11.5px] tnum flex-shrink-0 ${isCurrent ? 'text-brand' : 'text-gray-400'}`}>
            {done && stageData.duration != null
              ? `${stageData.duration.toFixed(1)}s`
              : isCurrent ? 'in progress'
              : 'queued'}
          </span>
        </div>
        {stageData.details && (
          <div className={`text-xs mt-1 leading-relaxed ${isCurrent ? 'text-brand/75' : 'text-gray-500'}`}>
            {stageData.details}
          </div>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value }) {
  return (
    <div>
      <div className="text-gray-400 mb-0.5 text-[11px] uppercase tracking-wide font-medium">{label}</div>
      <div className="font-semibold text-ink tnum">{value}</div>
    </div>
  )
}
