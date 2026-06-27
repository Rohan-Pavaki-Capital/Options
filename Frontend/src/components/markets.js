// Shared market metadata — drives the sidebar navigation and the main-area
// headers. Keys match the internal `mode` strings used by App/UploadScreen;
// labels are END-USER COUNTRY NAMES (no data-source / library names in the UI).
// The underlying source/provider is shown only as a small provenance footnote
// inside each panel.

export const COUNTRY_META = {
  edgar:     { label: 'United States', code: 'US', region: 'Americas' },
  canada:    { label: 'Canada',        code: 'CA', region: 'Americas' },
  brazil:    { label: 'Brazil',        code: 'BR', region: 'Americas' },
  mexico:    { label: 'Mexico',        code: 'MX', region: 'Americas' },
  uk:        { label: 'United Kingdom',code: 'GB', region: 'Europe' },
  germany:   { label: 'Germany',       code: 'DE', region: 'Europe' },
  denmark:   { label: 'Denmark',       code: 'DK', region: 'Europe' },
  eu:        { label: 'All European Countries', code: 'EU', region: 'Europe' },
  japan:     { label: 'Japan',         code: 'JP', region: 'Asia-Pacific' },
  korea:     { label: 'South Korea',   code: 'KR', region: 'Asia-Pacific' },
  china:     { label: 'China',         code: 'CN', region: 'Asia-Pacific' },
  hongkong:  { label: 'Hong Kong',     code: 'HK', region: 'Asia-Pacific' },
  india:     { label: 'India',         code: 'IN', region: 'Asia-Pacific' },
  taiwan:    { label: 'Taiwan',        code: 'TW', region: 'Asia-Pacific' },
  indonesia: { label: 'Indonesia',     code: 'ID', region: 'Asia-Pacific' },
  malaysia:  { label: 'Malaysia',      code: 'MY', region: 'Asia-Pacific' },
  thailand:  { label: 'Thailand',      code: 'TH', region: 'Asia-Pacific' },
  singapore: { label: 'Singapore',     code: 'SG', region: 'Asia-Pacific' },
  israel:    { label: 'Israel',        code: 'IL', region: 'Asia-Pacific' },
}

// Display order of regions in the sidebar.
export const REGIONS = ['Americas', 'Europe', 'Asia-Pacific']

// Country keys grouped by region, preserving the order declared above.
export const COUNTRIES_BY_REGION = REGIONS.map((region) => ({
  region,
  keys: Object.keys(COUNTRY_META).filter((k) => COUNTRY_META[k].region === region),
}))

// All country keys sorted alphabetically by display label (drives the dropdown nav).
export const COUNTRIES_ALPHA = Object.keys(COUNTRY_META).sort((a, b) =>
  COUNTRY_META[a].label.localeCompare(COUNTRY_META[b].label)
)
