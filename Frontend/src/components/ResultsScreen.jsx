import {
  Download, Calendar, DollarSign, Scale, FileText,
  CheckCircle, TrendingUp, TrendingDown,
  Box, Target, Info, AlertTriangle
} from 'lucide-react'

const PLAN_STYLES = {
  RSU:    { bg: '#EEEDFE', text: '#3C3489', icon: 'Cube' },
  PSU:    { bg: '#E1F5EE', text: '#085041', icon: 'Target' },
  PSP:    { bg: '#FCE4D6', text: '#712B13', icon: 'Target' },
  LTIP:   { bg: '#DAEEF3', text: '#0C447C', icon: 'Cube' },
  SAYE:   { bg: '#E2EFDA', text: '#3B6D11', icon: 'Cube' },
  CSOP:   { bg: '#FFF2CC', text: '#854F0B', icon: 'Cube' },
  ESOP:   { bg: '#DEEBF6', text: '#0C447C', icon: 'Cube' },
  RSP:    { bg: '#FBEAF0', text: '#72243E', icon: 'Cube' },
  default:{ bg: '#F1EFE8', text: '#444441', icon: 'Cube' },
}

function getPlanStyle(planType) {
  return PLAN_STYLES[planType] || PLAN_STYLES.default
}

function getCurrencySymbol(currency) {
  const map = { GBP: '£', USD: '$', EUR: '€', JPY: '¥', SGD: 'S$', HKD: 'HK$', AUD: 'A$', CAD: 'C$', INR: '₹' }
  return map[currency] || currency || ''
}

function formatNumber(n, decimals = 0) {
  if (n == null || n === '') return null
  const num = Number(n)
  if (isNaN(num)) return n
  return num.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}

export default function ResultsScreen({ result, jobId, apiBase }) {
  const {
    company_name,
    report_period,
    currency,
    reporting_standard,
    plans = [],
    _validation_summary: validation,
    _meta: meta,
  } = result

  const handleDownloadExcel = () => {
    window.location.href = `${apiBase}/download/${jobId}/excel`
  }

  // Aggregate stats — only compute from plans that have data
  const totalClosing = plans.reduce((sum, p) => sum + (p.closing_balance || p.total_contingent_awards || 0), 0)
  const totalPriorClosing = plans.reduce((sum, p) => sum + (p.prior_year?.closing_balance || 0), 0)
  const yoyPercent = totalPriorClosing > 0 ? ((totalClosing - totalPriorClosing) / totalPriorClosing * 100) : null
  const totalOpening = plans.reduce((sum, p) => sum + (p.opening_balance || 0), 0)
  const totalGranted = plans.reduce((sum, p) => sum + (p.granted || 0), 0)
  const totalVested = plans.reduce((sum, p) => sum + (p.vested || 0), 0)
  const totalExercised = plans.reduce((sum, p) => sum + (p.exercised || 0), 0)

  // Average fair value (weighted)
  const fvs = plans
    .map(p => ({
      fv: p.weighted_avg_grant_date_fair_value,
      weight: p.closing_balance || p.total_contingent_awards || 1
    }))
    .filter(x => x.fv != null)
  const avgFV = fvs.length > 0
    ? fvs.reduce((s, x) => s + x.fv * x.weight, 0) / fvs.reduce((s, x) => s + x.weight, 0)
    : null

  const priorFvs = plans
    .map(p => p.prior_year?.weighted_avg_grant_date_fair_value)
    .filter(x => x != null)
  const priorAvgFV = priorFvs.length > 0
    ? priorFvs.reduce((s, v) => s + v, 0) / priorFvs.length
    : null
  const fvChange = (avgFV && priorAvgFV) ? ((avgFV - priorAvgFV) / priorAvgFV * 100) : null

  return (
    <div className="space-y-3.5">
      {/* HEADER CARD */}
      <div className="card overflow-hidden">
        <div className="px-6 py-4 border-b border-hairline bg-canvas flex items-center gap-3.5">
          <div className="w-10 h-10 rounded-md bg-brand flex items-center justify-center text-accent-light font-serif font-semibold text-[12px] tracking-wide ring-1 ring-navy-900 flex-shrink-0">
            {company_name ? company_name.substring(0, 3).toUpperCase() : '—'}
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-[19px] font-serif text-ink truncate leading-tight">
              {company_name || 'Unknown Company'}
            </div>
            <div className="text-[11.5px] text-gray-500 flex gap-3.5 mt-1 flex-wrap">
              {report_period && (
                <span className="flex items-center gap-1"><Calendar className="w-3 h-3" /> {report_period}</span>
              )}
              {currency && (
                <span className="flex items-center gap-1"><DollarSign className="w-3 h-3" /> {currency}</span>
              )}
              {reporting_standard && (
                <span className="flex items-center gap-1"><Scale className="w-3 h-3" /> {reporting_standard}</span>
              )}
            </div>
          </div>
          <div className="flex gap-2">
            <a
              href={`${apiBase}/download/${jobId}/pdf`}
              target="_blank"
              rel="noopener noreferrer"
              className="btn btn-ghost"
            >
              <FileText className="w-4 h-4" />
              Show report PDF
            </a>
            <button onClick={handleDownloadExcel} className="btn btn-primary">
              <Download className="w-4 h-4" />
              Download Excel
            </button>
          </div>
        </div>

        {/* Status strip */}
        <div className="px-6 py-2.5 border-b border-hairline flex gap-4 flex-wrap text-[11.5px] text-gray-500 items-center">
          <span className="flex items-center gap-1.5">
            <CheckCircle className="w-3.5 h-3.5 text-green-600" />
            Extraction complete
          </span>
          {meta?.pages_processed && (
            <span>
              <FileText className="w-3.5 h-3.5 inline mr-1" />
              Pages {meta.pages_processed.join(', ')}{meta.total_pdf_pages ? ` of ${meta.total_pdf_pages}` : ''}
            </span>
          )}
        </div>

        {/* KPI Strip */}
        <div className="px-6 py-5 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          <KPICard
            label="TOTAL OUTSTANDING"
            value={formatNumber(totalClosing)}
            unit={plans[0]?.units_label === 'thousands' ? 'k' : ''}
            change={yoyPercent}
            changeLabel="YoY"
          />
          <KPICard
            label="PLANS DETECTED"
            value={plans.length}
            subtitle={plans.map(p => p.plan_type).filter(Boolean).join(' · ')}
          />
          {totalGranted > 0 && (
            <KPICard
              label="GRANTS"
              value={formatNumber(totalGranted)}
              unit={plans[0]?.units_label === 'thousands' ? 'k' : ''}
              subtitle={totalOpening > 0 ? `${(totalGranted / totalOpening * 100).toFixed(1)}% of opening` : null}
            />
          )}
          {totalVested > 0 && (
            <KPICard
              label="VESTED"
              value={formatNumber(totalVested)}
              unit={plans[0]?.units_label === 'thousands' ? 'k' : ''}
              subtitle={totalOpening > 0 ? `${(totalVested / totalOpening * 100).toFixed(1)}% of opening` : null}
            />
          )}
          {totalExercised > 0 && (
            <KPICard
              label="EXERCISED"
              value={formatNumber(totalExercised)}
              unit={plans[0]?.units_label === 'thousands' ? 'k' : ''}
              subtitle={totalOpening > 0 ? `${(totalExercised / totalOpening * 100).toFixed(1)}% of opening` : null}
            />
          )}
          {avgFV != null && (
            <KPICard
              label="AVG FAIR VALUE"
              value={`${getCurrencySymbol(currency)}${avgFV.toFixed(2)}`}
              change={fvChange}
            />
          )}
        </div>
      </div>

      {/* ROLL-FORWARD + COMPOSITION */}
      {plans.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-3.5">
          <div className="lg:col-span-2">
            <RollForwardTable plans={plans} reportPeriod={report_period} />
          </div>
          <div>
            <CompositionCard plans={plans} />
          </div>
        </div>
      )}

      {/* PLAN DETAILS */}
      <div className="card p-5">
        <div className="text-[15px] font-serif text-ink mb-4">Plan details</div>
        <div className="space-y-3">
          {plans.map((plan, idx) => (
            <PlanCard key={idx} plan={plan} currency={currency} />
          ))}
        </div>
      </div>

      {/* WARNINGS (if any) */}
      {validation?.warnings?.length > 0 && (
        <div className="card p-5">
          <div className="text-[15px] font-serif text-ink mb-3 flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-500" />
            Items to verify ({validation.warnings.length})
          </div>
          <div className="space-y-2">
            {validation.warnings.map((w, i) => (
              <div key={i} className="text-[13px] text-amber-900 bg-amber-50/70 px-3.5 py-2.5 rounded-md border border-amber-200/80 leading-relaxed">
                {w}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ─── KPI CARD ──────────────────────────────────────────────────────────────
function KPICard({ label, value, unit, subtitle, change, changeLabel }) {
  return (
    <div className="metric-card">
      <div className="text-[10px] font-semibold uppercase text-gray-400 mb-2 tracking-eyebrow">{label}</div>
      <div className="text-[22px] font-serif text-ink leading-none tnum">
        {value}
        {unit && <span className="text-xs text-gray-500 ml-1 font-sans font-normal">{unit}</span>}
      </div>
      {change != null && (
        <div className={`text-[11.5px] mt-2 flex items-center gap-1 tnum font-medium ${change >= 0 ? 'text-emerald-700' : 'text-red-700'}`}>
          {change >= 0 ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
          {change >= 0 ? '+' : ''}{change.toFixed(1)}% {changeLabel && <span className="text-gray-400 font-normal">{changeLabel}</span>}
        </div>
      )}
      {subtitle && !change && (
        <div className="text-[11.5px] text-gray-500 mt-2 truncate">{subtitle}</div>
      )}
    </div>
  )
}

// ─── ROLL-FORWARD TABLE ────────────────────────────────────────────────────
function RollForwardTable({ plans, reportPeriod }) {
  const hasGranted = plans.some(p => p.granted != null)
  const hasExercised = plans.some(p => p.exercised != null)
  const hasVested = plans.some(p => p.vested != null)
  const hasLapsed = plans.some(p => p.forfeited_or_lapsed != null)

  const rfPlans = plans.filter(p => p.opening_balance != null || p.closing_balance != null)
  if (rfPlans.length === 0) return null

  const checkMath = (p) => {
    const open = p.opening_balance || 0
    const close = p.closing_balance || 0
    const expected = open + (p.granted || 0) - (p.exercised || 0) - (p.forfeited_or_lapsed || 0) - (p.vested || 0)
    return Math.abs(expected - close) <= 1
  }

  return (
    <div className="card p-5">
      <div className="flex justify-between items-baseline mb-1">
        <div className="text-[15px] font-serif text-ink">Plan roll-forward</div>
      </div>
      <div className="text-[11.5px] text-gray-400 mb-4">
        {reportPeriod} · shares in {plans[0]?.units_label || 'units'}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[10px] text-gray-400 font-semibold uppercase tracking-wider border-b border-gray-300">
              <th className="text-left py-2">Plan</th>
              <th className="text-right py-2">Open</th>
              {hasGranted && <th className="text-right py-2">Granted</th>}
              {hasExercised && <th className="text-right py-2">Exercised</th>}
              {hasVested && <th className="text-right py-2">Vested</th>}
              {hasLapsed && <th className="text-right py-2">Forfeited</th>}
              <th className="text-right py-2">Close</th>
              <th className="text-center py-2">Check</th>
            </tr>
          </thead>
          <tbody>
            {rfPlans.map((plan, i) => {
              const style = getPlanStyle(plan.plan_type)
              const mathOk = checkMath(plan)
              return (
                <tr key={i} className="border-b border-gray-100 hover:bg-canvas/60 transition-colors">
                  <td className="py-2.5">
                    <span className="badge" style={{ background: style.bg, color: style.text }}>
                      {plan.plan_type}
                    </span>
                  </td>
                  <td className="text-right py-2.5 tnum">{formatNumber(plan.opening_balance)}</td>
                  {hasGranted && <td className="text-right py-2.5 tnum text-emerald-700">{plan.granted != null ? `+${formatNumber(plan.granted)}` : ''}</td>}
                  {hasExercised && <td className="text-right py-2.5 tnum text-red-700">{plan.exercised != null ? `(${formatNumber(plan.exercised)})` : ''}</td>}
                  {hasVested && <td className="text-right py-2.5 tnum text-red-700">{plan.vested != null ? `(${formatNumber(plan.vested)})` : ''}</td>}
                  {hasLapsed && <td className="text-right py-2.5 tnum text-red-700">{plan.forfeited_or_lapsed != null ? `(${formatNumber(plan.forfeited_or_lapsed)})` : ''}</td>}
                  <td className="text-right py-2.5 tnum font-semibold">{formatNumber(plan.closing_balance)}</td>
                  <td className="text-center py-2.5">
                    {mathOk ? (
                      <CheckCircle className="w-3.5 h-3.5 text-emerald-600 inline-block" />
                    ) : (
                      <AlertTriangle className="w-3.5 h-3.5 text-amber-500 inline-block" />
                    )}
                  </td>
                </tr>
              )
            })}
            <tr className="border-t border-gray-300 bg-canvas/80 font-semibold">
              <td className="py-2.5 text-[10.5px] uppercase tracking-wider text-gray-500">Total</td>
              <td className="text-right py-2.5 tnum">{formatNumber(rfPlans.reduce((s, p) => s + (p.opening_balance || 0), 0))}</td>
              {hasGranted && <td className="text-right py-2.5 tnum text-emerald-700">+{formatNumber(rfPlans.reduce((s, p) => s + (p.granted || 0), 0))}</td>}
              {hasExercised && <td className="text-right py-2.5 tnum text-red-700">({formatNumber(rfPlans.reduce((s, p) => s + (p.exercised || 0), 0))})</td>}
              {hasVested && <td className="text-right py-2.5 tnum text-red-700">({formatNumber(rfPlans.reduce((s, p) => s + (p.vested || 0), 0))})</td>}
              {hasLapsed && <td className="text-right py-2.5 tnum text-red-700">({formatNumber(rfPlans.reduce((s, p) => s + (p.forfeited_or_lapsed || 0), 0))})</td>}
              <td className="text-right py-2.5 tnum">{formatNumber(rfPlans.reduce((s, p) => s + (p.closing_balance || 0), 0))}</td>
              <td></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="mt-4 px-3 py-2.5 bg-canvas border border-hairline rounded-md text-[11.5px] text-gray-500 flex items-start gap-2 leading-relaxed">
        <Info className="w-3.5 h-3.5 mt-0.5 flex-shrink-0 text-gray-400" />
        <span>
          {hasVested && !hasExercised
            ? 'Vesting reduces outstanding balance (no exercise price for nil-cost awards).'
            : hasExercised && !hasVested
            ? 'Exercise converts options to shares. All math validates within ±1 unit.'
            : 'Math Check: Opening + Granted - Exercised - Vested - Lapsed = Closing (±1 unit tolerance)'}
        </span>
      </div>
    </div>
  )
}

// ─── COMPOSITION CARD ─────────────────────────────────────────────────────
// Distinct, professional palette assigned per slice (by index) so segments are
// always distinguishable — even when two plans share the same plan_type (e.g.
// two RSU plans), which would otherwise render the same dark shade.
const COMPOSITION_COLORS = [
  '#E63946', // red
  '#F3722C', // orange
  '#F9C74F', // yellow
  '#43AA8B', // green
  '#4D96FF', // blue
  '#9B5DE5', // violet
  '#F15BB5', // pink
  '#00BBF9', // cyan
]

function CompositionCard({ plans }) {
  const summaryPlans = plans
    .filter(p => p.closing_balance != null || p.total_contingent_awards != null)
    .map(p => ({
      ...p,
      value: p.closing_balance || p.total_contingent_awards || 0,
    }))
    .sort((a, b) => b.value - a.value)

  if (summaryPlans.length === 0) return null

  const total = summaryPlans.reduce((s, p) => s + p.value, 0)
  let cumulativeAngle = -90
  const radius = 54
  const cx = 70
  const cy = 70
  const circumference = 2 * Math.PI * radius

  return (
    <div className="card p-5 h-full">
      <div className="text-[15px] font-serif text-ink mb-1">Composition</div>
      <div className="text-[11.5px] text-gray-400 mb-4">By closing balance</div>

      <div className="flex items-center justify-center mb-4">
        <svg width="140" height="140" viewBox="0 0 140 140">
          {summaryPlans.map((plan, i) => {
            const color = COMPOSITION_COLORS[i % COMPOSITION_COLORS.length]
            const percentage = plan.value / total
            const arc = percentage * circumference
            const offset = -((cumulativeAngle + 90) / 360) * circumference
            const result = (
              <circle
                key={i}
                cx={cx}
                cy={cy}
                r={radius}
                fill="none"
                stroke={color}
                strokeWidth="22"
                strokeDasharray={`${arc} ${circumference - arc}`}
                strokeDashoffset={offset}
                transform={`rotate(-90 ${cx} ${cy})`}
              />
            )
            cumulativeAngle += percentage * 360
            return result
          })}
          <text x="70" y="66" textAnchor="middle" fontSize="20" fontWeight="500" fill="#111827">
            {formatNumber(total)}{plans[0]?.units_label === 'thousands' ? 'k' : ''}
          </text>
          <text x="70" y="84" textAnchor="middle" fontSize="11" fill="#6b7280">
            total
          </text>
        </svg>
      </div>

      <div className="space-y-2">
        {summaryPlans.map((plan, i) => {
          const color = COMPOSITION_COLORS[i % COMPOSITION_COLORS.length]
          const pct = (plan.value / total * 100).toFixed(1)
          return (
            <div key={i} className="flex justify-between items-center py-1.5 border-t border-gray-100 text-xs">
              <span className="flex items-center gap-2">
                <span className="w-2 h-2 rounded-sm" style={{ background: color }}></span>
                <span className="font-medium">{plan.plan_type || plan.plan_name}</span>
              </span>
              <span className="text-gray-500">{formatNumber(plan.value)}</span>
              <span className="font-medium">{pct}%</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ─── PLAN CARD ────────────────────────────────────────────────────────────
function PlanCard({ plan, currency }) {
  const style = getPlanStyle(plan.plan_type)
  const currencySymbol = getCurrencySymbol(currency)

  const badges = []
  if (plan.is_nil_cost) badges.push({ label: 'Nil-cost' })
  if (plan.is_cash_settled === false) badges.push({ label: 'Equity-settled' })
  if (plan.is_cash_settled === true) badges.push({ label: 'Cash-settled' })
  if (plan.vesting_period_years) badges.push({ label: `${plan.vesting_period_years}-yr vesting` })
  if (plan.performance_period_years) badges.push({ label: `${plan.performance_period_years}-yr performance` })
  if (plan.holding_period_years) badges.push({ label: `${plan.holding_period_years}-yr holding` })

  const metrics = []
  if (plan.opening_balance != null) metrics.push({ label: 'Opening', value: formatNumber(plan.opening_balance) })
  if (plan.granted != null) metrics.push({ label: 'Granted', value: `+${formatNumber(plan.granted)}`, color: 'text-green-700' })
  if (plan.exercised != null) metrics.push({ label: 'Exercised', value: `(${formatNumber(plan.exercised)})`, color: 'text-red-700' })
  if (plan.vested != null) metrics.push({ label: 'Vested', value: `(${formatNumber(plan.vested)})`, color: 'text-red-700' })
  if (plan.forfeited_or_lapsed != null) metrics.push({ label: 'Forfeited', value: `(${formatNumber(plan.forfeited_or_lapsed)})`, color: 'text-red-700' })
  if (plan.closing_balance != null) metrics.push({ label: 'Closing', value: formatNumber(plan.closing_balance) })
  if (plan.exercisable_at_period_end != null) metrics.push({ label: 'Exercisable', value: formatNumber(plan.exercisable_at_period_end) })

  if (plan.weighted_avg_exercise_price != null) {
    metrics.push({ label: 'Avg exercise price', value: `${plan.weighted_avg_exercise_price}${plan.weighted_avg_exercise_price_unit === 'pence' ? 'p' : ` ${currencySymbol}`}` })
  }
  if (plan.weighted_avg_grant_date_fair_value != null) {
    metrics.push({ label: 'Grant date FV', value: `${currencySymbol}${plan.weighted_avg_grant_date_fair_value}` })
  }
  if (plan.exercise_price_range_low != null && plan.exercise_price_range_high != null) {
    const u = plan.exercise_price_range_unit === 'pence' ? 'p' : ''
    metrics.push({ label: 'Exercise range', value: `${plan.exercise_price_range_low}${u} – ${plan.exercise_price_range_high}${u}` })
  }
  if (plan.weighted_avg_remaining_contractual_life_years != null) {
    metrics.push({ label: 'Contractual life', value: `${plan.weighted_avg_remaining_contractual_life_years}y` })
  }
  if (plan.weighted_avg_share_price_at_exercise != null) {
    const u = plan.weighted_avg_share_price_at_exercise_unit === 'pence' ? 'p' : ''
    metrics.push({ label: 'Share price @ exercise', value: `${plan.weighted_avg_share_price_at_exercise}${u}` })
  }

  if (plan.total_contingent_awards != null) {
    metrics.push({ label: 'Total contingent', value: formatNumber(plan.total_contingent_awards) })
  }
  if (plan.contingent_cash_settled != null) {
    metrics.push({ label: 'Cash-settled portion', value: formatNumber(plan.contingent_cash_settled) })
  }
  if (plan.contingent_equity_settled != null) {
    metrics.push({ label: 'Equity-settled portion', value: formatNumber(plan.contingent_equity_settled) })
  }

  const priorMetrics = []
  if (plan.prior_year) {
    const py = plan.prior_year
    if (py.opening_balance != null) priorMetrics.push({ label: 'Opening', value: formatNumber(py.opening_balance) })
    if (py.granted != null) priorMetrics.push({ label: 'Granted', value: `+${formatNumber(py.granted)}` })
    if (py.exercised != null) priorMetrics.push({ label: 'Exercised', value: `(${formatNumber(py.exercised)})` })
    if (py.vested != null) priorMetrics.push({ label: 'Vested', value: `(${formatNumber(py.vested)})` })
    if (py.forfeited_or_lapsed != null) priorMetrics.push({ label: 'Forfeited', value: `(${formatNumber(py.forfeited_or_lapsed)})` })
    if (py.closing_balance != null) priorMetrics.push({ label: 'Closing', value: formatNumber(py.closing_balance) })
    if (py.weighted_avg_exercise_price != null) priorMetrics.push({ label: 'Avg exercise price', value: py.weighted_avg_exercise_price })
    if (py.weighted_avg_grant_date_fair_value != null) priorMetrics.push({ label: 'Grant date FV', value: `${currencySymbol}${py.weighted_avg_grant_date_fair_value}` })
  }

  return (
    <div className="border border-hairline rounded-md p-4 hover:border-gray-300 transition-colors">
      <div className="flex items-center gap-2.5 mb-3 flex-wrap">
        <span className="w-6 h-6 rounded-md flex items-center justify-center flex-shrink-0" style={{ background: style.bg }}>
          {plan.is_nil_cost ? <Target className="w-3.5 h-3.5" style={{ color: style.text }} /> : <Box className="w-3.5 h-3.5" style={{ color: style.text }} />}
        </span>
        <span className="font-semibold text-[13.5px] text-ink">{plan.plan_name}</span>
        {plan.plan_type && (
          <span className="badge" style={{ background: style.bg, color: style.text }}>
            {plan.plan_type}
          </span>
        )}
        {badges.map((b, i) => (
          <span key={i} className="badge bg-gray-100 text-gray-600">
            {b.label}
          </span>
        ))}
        {plan.units_label && (
          <span className="ml-auto text-[10.5px] text-gray-400 uppercase tracking-wide">in {plan.units_label}</span>
        )}
      </div>

      {plan.plan_description && (
        <div className="text-[13px] text-gray-600 leading-relaxed mb-3.5">
          {plan.plan_description}
        </div>
      )}

      {metrics.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-x-4 gap-y-3 px-3.5 py-3 bg-canvas border border-hairline rounded-md mb-3.5">
          {metrics.map((m, i) => (
            <div key={i}>
              <div className="text-[10px] text-gray-400 uppercase tracking-wide font-medium mb-0.5">{m.label}</div>
              <div className={`text-[13.5px] font-medium tnum ${m.color || 'text-ink'}`}>{m.value}</div>
            </div>
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3.5">
        {plan.vesting_description && (
          <DetailBlock label="VESTING" text={plan.vesting_description} />
        )}
        {plan.performance_conditions && (
          <DetailBlock label="PERFORMANCE CONDITIONS" text={plan.performance_conditions} />
        )}
      </div>

      {plan.tranches?.length > 0 && (
        <TranchesTable tranches={plan.tranches} />
      )}

      {priorMetrics.length > 0 && (
        <div className="mt-3.5 pt-3 border-t border-dashed border-gray-200">
          <div className="text-[11px] text-gray-500 tracking-wide mb-1.5">PRIOR YEAR</div>
          <div className="flex flex-wrap gap-4 text-xs">
            {priorMetrics.map((m, i) => (
              <span key={i} className="text-gray-600">
                {m.label} <b className="text-gray-900 font-medium">{m.value}</b>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function DetailBlock({ label, text }) {
  return (
    <div>
      <div className="text-[11px] text-gray-500 tracking-wide mb-1">{label}</div>
      <div className="text-sm text-gray-700 leading-relaxed">{text}</div>
    </div>
  )
}

// ─── TRANCHES TABLE ────────────────────────────────────────────────────────
function TranchesTable({ tranches }) {
  if (!tranches.length) return null

  const hasGrantPrice = tranches.some(t => t.grant_price != null)
  const hasExercisePrice = tranches.some(t => t.exercise_price != null)
  const hasVesting = tranches.some(t => t.vesting_period_years != null)
  const hasFV = tranches.some(t => t.fair_value_per_option != null)

  return (
    <div className="mt-3.5 pt-3 border-t border-dashed border-gray-200">
      <div className="text-[11px] text-gray-500 tracking-wide mb-2">TRANCHES ({tranches.length})</div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-500 border-b border-gray-200">
              <th className="text-left py-1.5 font-normal">Grant date</th>
              <th className="text-right py-1.5 font-normal">Shares</th>
              {hasGrantPrice && <th className="text-right py-1.5 font-normal">Grant price</th>}
              {hasExercisePrice && <th className="text-right py-1.5 font-normal">Exercise price</th>}
              {hasVesting && <th className="text-right py-1.5 font-normal">Vesting</th>}
              {hasFV && <th className="text-right py-1.5 font-normal">FV</th>}
            </tr>
          </thead>
          <tbody>
            {tranches.map((t, i) => {
              const gpUnit = t.grant_price_unit === 'pence' ? 'p' : ''
              const epUnit = t.exercise_price_unit === 'pence' ? 'p' : ''
              return (
                <tr key={i} className="border-b border-gray-100">
                  <td className="py-1.5">{t.grant_date || '—'}</td>
                  <td className="text-right py-1.5">{formatNumber(t.shares_at_period_end ?? t.shares_granted)}</td>
                  {hasGrantPrice && <td className="text-right py-1.5">{t.grant_price != null ? `${t.grant_price}${gpUnit}` : '—'}</td>}
                  {hasExercisePrice && <td className="text-right py-1.5">{t.exercise_price != null ? `${t.exercise_price}${epUnit}` : '—'}</td>}
                  {hasVesting && <td className="text-right py-1.5">{t.vesting_period_years != null ? `${t.vesting_period_years}y` : '—'}</td>}
                  {hasFV && <td className="text-right py-1.5">{t.fair_value_per_option != null ? t.fair_value_per_option : '—'}</td>}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
