import { AlertCircle, FileText, FileSearch } from 'lucide-react'

// Centered popup for terminal, user-facing outcomes (e.g. "no options data
// available", "report not found"). Friendly copy comes from errorCopy.js — this
// component is presentation only. Click the backdrop or the button to dismiss.
// When `pdfUrl` is provided, a link to view the fetched source report is shown.
// When `altLabel` + `onAlt` are provided (EU tab: a downloaded interim report is
// available), a primary "Try the …" action is offered above the dismiss button.
export default function ResultModal({ open, title, body, pdfUrl, altLabel, onAlt, onClose }) {
  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="w-full max-w-md rounded-lg bg-white shadow-panel border border-hairline p-7 text-center animate-enter"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="w-11 h-11 mx-auto mb-4 rounded-full bg-amber-50 border border-amber-200 flex items-center justify-center">
          <AlertCircle className="w-5 h-5 text-amber-500" />
        </div>
        <h2 className="text-[17px] font-serif text-ink mb-2">{title}</h2>
        <p className="text-[13px] text-gray-600 leading-relaxed mb-6">{body}</p>
        {pdfUrl && (
          <a
            href={pdfUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="btn btn-ghost w-full justify-center mb-2"
          >
            <FileText className="w-4 h-4" />
            View the report PDF
          </a>
        )}
        {altLabel && onAlt ? (
          <>
            <button onClick={onAlt} className="btn btn-primary w-full justify-center mb-2">
              <FileSearch className="w-4 h-4" />
              Try the {altLabel}
            </button>
            <button onClick={onClose} className="btn btn-ghost w-full justify-center">
              No thanks
            </button>
          </>
        ) : (
          <button onClick={onClose} className="btn btn-primary w-full justify-center">
            OK
          </button>
        )}
      </div>
    </div>
  )
}
