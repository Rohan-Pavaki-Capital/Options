/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,jsx,ts,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        serif: ['"Source Serif 4"', 'Georgia', 'Cambria', 'serif'],
      },
      colors: {
        // Primary institutional navy. `brand` keeps its name for backward
        // compatibility (bg-brand / focus:ring-brand used across the app).
        brand: {
          DEFAULT: '#14233A',
          light: '#1F3A5F',
          dark: '#0B1626',
          pale: '#EDF1F6',
        },
        navy: {
          900: '#0B1626',
          800: '#14233A',
          700: '#1F3A5F',
          600: '#2B4C7E',
          500: '#3E6299',
          pale: '#EDF1F6',
        },
        // Restrained muted-gold accent — used sparingly (rules, eyebrows).
        accent: {
          DEFAULT: '#B08D57',
          dark: '#8A6D3F',
          light: '#C9A86E',
          pale: '#F4EEE2',
        },
        canvas: '#F7F6F2',   // warm off-white "report paper" background
        paper: '#FFFFFF',
        hairline: '#E6E2D8', // warm hairline borders
        ink: '#14233A',
      },
      boxShadow: {
        card: '0 1px 3px rgba(20, 35, 58, 0.06), 0 1px 2px rgba(20, 35, 58, 0.04)',
        panel: '0 4px 24px rgba(20, 35, 58, 0.08)',
      },
      letterSpacing: {
        eyebrow: '0.18em',
      },
    },
  },
  plugins: [],
}
