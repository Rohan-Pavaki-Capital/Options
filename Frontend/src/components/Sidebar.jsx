import { useState, useEffect } from 'react'
import { Search, CloudUpload, FlaskConical, ChevronDown } from 'lucide-react'
import { COUNTRY_META, COUNTRIES_BY_REGION } from './markets'

// Left navigation for the console layout. Primary action on top (Upload a
// report), then countries grouped by region, then a collapsible Advanced
// section holding the any-market company search and the diagnostic Testing
// tool. `mode` is the active source key; selecting one calls onSelect(key).
export default function Sidebar({ mode, onSelect, disabled }) {
  // Auto-open Advanced when one of its tools is the active mode (e.g. the app
  // lands on the company search by default) so the active item isn't hidden.
  const [advancedOpen, setAdvancedOpen] = useState(
    mode === 'diamond' || mode === 'testing'
  )

  useEffect(() => {
    if (mode === 'diamond' || mode === 'testing') setAdvancedOpen(true)
  }, [mode])

  return (
    <nav className="w-full lg:w-64 lg:flex-shrink-0 lg:border-r lg:border-hairline lg:min-h-[calc(100vh-61px)] bg-paper">
      <div className="p-4 lg:p-5 space-y-5">
        {/* New extraction heading */}
        <div>
          <div className="eyebrow mb-3">New Extraction</div>
          <div className="space-y-1">
            <PrimaryItem
              active={mode === 'upload'}
              disabled={disabled}
              onClick={() => onSelect('upload')}
              icon={<CloudUpload className="w-4 h-4" />}
              label="Upload a report"
              hint="PDF you already have"
            />
          </div>
        </div>

        {/* Country / market — continents shown inline, each expandable */}
        <div>
          <div className="eyebrow mb-2">Country / Market</div>
          <RegionAccordion mode={mode} onSelect={onSelect} disabled={disabled} />
        </div>

        {/* Advanced — collapsible: any-market search + diagnostic Testing tool */}
        <div className="pt-1 border-t border-hairline">
          <button
            type="button"
            onClick={() => setAdvancedOpen((v) => !v)}
            className="w-full flex items-center justify-between text-[11px] font-semibold uppercase tracking-eyebrow text-gray-400 hover:text-gray-600 py-2 transition-colors"
          >
            Advanced
            <ChevronDown className={`w-3.5 h-3.5 transition-transform ${advancedOpen ? 'rotate-180' : ''}`} />
          </button>
          {advancedOpen && (
            <div className="space-y-1 mt-1">
              <PrimaryItem
                active={mode === 'diamond'}
                disabled={disabled}
                onClick={() => onSelect('diamond')}
                icon={<Search className="w-4 h-4" />}
                label="Search any company"
                hint="Auto-find · any market"
              />
              <CountryItem
                active={mode === 'testing'}
                disabled={disabled}
                onClick={() => onSelect('testing')}
                icon={<FlaskConical className="w-3.5 h-3.5" />}
                label="Testing (diagnostic)"
              />
            </div>
          )}
        </div>
      </div>
    </nav>
  )
}

function PrimaryItem({ active, disabled, onClick, icon, label, hint }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`
        w-full flex items-start gap-2.5 px-3 py-2.5 rounded-md text-left transition-all duration-150
        disabled:opacity-50 disabled:cursor-not-allowed
        ${active
          ? 'bg-brand text-white shadow-sm'
          : 'bg-paper text-ink border border-hairline hover:border-gray-300 hover:shadow-card'}
      `}
    >
      <span className={`mt-0.5 ${active ? 'text-accent-light' : 'text-accent-dark'}`}>{icon}</span>
      <span className="min-w-0">
        <span className="block text-[13px] font-semibold leading-tight">{label}</span>
        <span className={`block text-[11px] mt-0.5 ${active ? 'text-white/60' : 'text-gray-400'}`}>
          {hint}
        </span>
      </span>
    </button>
  )
}

function CountryItem({ active, disabled, onClick, code, icon, label }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`
        w-full flex items-center gap-2.5 px-2.5 py-[7px] rounded-md text-[13px] transition-colors
        disabled:opacity-50 disabled:cursor-not-allowed
        ${active
          ? 'bg-brand/[0.06] text-ink font-semibold'
          : 'text-gray-600 hover:text-ink hover:bg-black/[0.03]'}
      `}
    >
      {code ? (
        <span className={`
          inline-flex items-center justify-center w-7 h-[18px] rounded-[3px] text-[9.5px] font-bold tracking-wider flex-shrink-0
          ${active ? 'bg-brand text-white' : 'bg-gray-200/70 text-gray-500'}
        `}>
          {code}
        </span>
      ) : (
        <span className={`w-7 flex justify-center flex-shrink-0 ${active ? 'text-accent-dark' : 'text-gray-400'}`}>
          {icon}
        </span>
      )}
      <span className="truncate">{label}</span>
    </button>
  )
}

// Country picker grouped by continent, shown INLINE in the sidebar: three collapsible
// continent sections (Americas / Europe / Asia-Pacific); expanding one reveals its
// countries (alphabetical within). `mode` is the active source key.
function RegionAccordion({ mode, onSelect, disabled }) {
  // Auto-expand the active country's continent (others start collapsed).
  const [openRegions, setOpenRegions] = useState(() => {
    const r = COUNTRY_META[mode] ? COUNTRY_META[mode].region : null
    return r ? { [r]: true } : {}
  })

  // Keep the active country's continent expanded when the selection changes.
  useEffect(() => {
    const r = COUNTRY_META[mode] ? COUNTRY_META[mode].region : null
    if (r) setOpenRegions((prev) => ({ ...prev, [r]: true }))
  }, [mode])

  // Continent -> alphabetical country keys.
  const groups = COUNTRIES_BY_REGION.map(({ region, keys }) => ({
    region,
    keys: [...keys].sort((a, b) => COUNTRY_META[a].label.localeCompare(COUNTRY_META[b].label)),
  }))

  const toggleRegion = (region) =>
    setOpenRegions((prev) => ({ ...prev, [region]: !prev[region] }))

  return (
    <div className="space-y-1">
      {groups.map(({ region, keys }) => (
        <div key={region}>
          <button
            type="button"
            disabled={disabled}
            onClick={() => toggleRegion(region)}
            className="w-full flex items-center justify-between px-2.5 py-2 rounded-md text-[10.5px] font-semibold uppercase tracking-eyebrow text-gray-500 hover:text-ink hover:bg-black/[0.03] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {region}
            <ChevronDown className={`w-3.5 h-3.5 text-gray-400 transition-transform duration-200 ${openRegions[region] ? 'rotate-180' : ''}`} />
          </button>
          {openRegions[region] && (
            <div className="space-y-px mt-0.5 mb-1.5 pl-1.5 ml-1 border-l border-hairline">
              {keys.map((key) => (
                <CountryItem
                  key={key}
                  active={mode === key}
                  disabled={disabled}
                  onClick={() => onSelect(key)}
                  code={COUNTRY_META[key].code}
                  label={COUNTRY_META[key].label}
                />
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
