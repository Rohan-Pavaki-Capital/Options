# Pavaki Options Extractor — Full Stack App

A production-ready FastAPI + React application for extracting share-based compensation data from annual reports.

```
┌────────────────────────────────────────────────┐
│   User uploads PDF                             │
│         ↓                                      │
│   React frontend (port 5173)                   │
│         ↓ POST /api/extract                    │
│   FastAPI backend (port 8000)                  │
│         ↓                                      │
│   Your extraction pipeline                     │
│   (extract_options.py + json_to_excel.py)      │
│         ↓                                      │
│   Results displayed + Excel download           │
└────────────────────────────────────────────────┘
```

---

## 📦 Project Structure

```
equity_extractor_app/
├── backend/
│   ├── main.py                  # FastAPI application
│   ├── requirements.txt         # Python dependencies
│   ├── .env.example             # Environment variables template
│   ├── extract_options.py       # ⚠ COPY YOUR PIPELINE HERE
│   ├── json_to_excel.py         # ⚠ COPY YOUR CONVERTER HERE
│   └── jobs/                    # Auto-created: job artifacts
│
└── frontend/
    ├── package.json
    ├── vite.config.js
    ├── tailwind.config.js
    ├── postcss.config.js
    ├── index.html
    └── src/
        ├── main.jsx
        ├── App.jsx              # State machine
        ├── index.css
        └── components/
            ├── UploadScreen.jsx
            ├── ProcessingScreen.jsx
            └── ResultsScreen.jsx
```

---

## 🚀 Quick Start

### Prerequisites

- Python 3.9+
- Node.js 18+
- npm or yarn

### Step 1: Set up the backend

```bash
cd backend

# Create virtual environment
python -m venv venv
source venv/bin/activate    # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy your extraction pipeline files here
# (from your earlier work)
cp /path/to/your/extract_options.py .
cp /path/to/your/json_to_excel.py .

# Set up environment variables
cp .env.example .env
# Edit .env with your API keys

# Run the backend
uvicorn main:app --reload --port 8000
```

Backend will be available at: **http://localhost:8000**
- API docs: http://localhost:8000/docs
- Health check: http://localhost:8000/api/health

### Step 2: Set up the frontend (in another terminal)

```bash
cd frontend

# Install dependencies
npm install

# Run dev server
npm run dev
```

Frontend will be available at: **http://localhost:5173**

### Step 3: Use it

1. Open http://localhost:5173 in your browser
2. Drag and drop a PDF annual report
3. Watch the 3-stage pipeline process it
4. Review the extracted data
5. Download the Excel report

---

## 🧪 Test Mode (no extraction pipeline yet)

If you haven't copied your extraction pipeline yet, the backend falls back to **mock mode** automatically. It will:
- Simulate the 3-stage pipeline with realistic timing
- Return mock Target Corporation data
- Generate a real Excel file (if `json_to_excel.py` is present)

This lets you test the full UI flow without needing API keys.

---

## 🔌 API Reference

### POST `/api/extract`
Upload a PDF and start extraction.

**Request:** multipart/form-data with `file` field
**Response:**
```json
{
  "job_id": "abc12345",
  "status": "queued",
  "filename": "annual_report.pdf",
  "file_size": 2456789
}
```

### GET `/api/job/{job_id}`
Get current job status (polled every 1s by frontend).

**Response:**
```json
{
  "job_id": "abc12345",
  "status": "processing",
  "progress": 65,
  "current_stage": "stage3_extraction",
  "stages": {
    "stage1_keywords": { "status": "completed", "duration": 2.1, "cost": 0 },
    "stage2_classifier": { "status": "completed", "duration": 4.3, "cost": 0.0647 },
    "stage3_extraction": { "status": "in_progress", ... },
    "...": "..."
  },
  "elapsed_seconds": 12.4,
  "estimated_remaining": 6.8,
  "cost_so_far": 0.0647
}
```

### GET `/api/result/{job_id}`
Get the final extraction JSON.

### GET `/api/download/{job_id}/excel`
Download the generated Excel file.

### GET `/api/download/{job_id}/json`
Download the raw JSON result.

### DELETE `/api/job/{job_id}`
Cancel a job and delete its files.

---

## 🎨 Frontend Architecture

The app is a single-page React application with a simple state machine:

```
idle → uploading → processing → completed
                       ↓            ↑
                     failed       (reset)
```

**Three main components:**

1. **`UploadScreen.jsx`** — Drag & drop, file picker, feature highlights
2. **`ProcessingScreen.jsx`** — Live pipeline progress with polling
3. **`ResultsScreen.jsx`** — All extracted data with smart field hiding

### Key features:
- ✅ **Smart rendering** — only shows fields that have data (no null clutter)
- ✅ **Auto-polling** — updates every 1 second during processing
- ✅ **Math validation** — green ✓ if roll-forward math is correct
- ✅ **Plan-aware** — color-codes by plan type (RSU, PSU, LTIP, etc.)
- ✅ **Multi-currency** — handles GBP, USD, EUR, SGD, etc.
- ✅ **Dynamic columns** — table columns adapt to data presence
- ✅ **Responsive** — works on desktop, tablet, mobile

---

## 🐳 Docker Deployment (Optional)

A `docker-compose.yml` setup is shown below — you can add this for production:

```yaml
version: '3.8'
services:
  backend:
    build: ./backend
    ports: ["8000:8000"]
    env_file: ./backend/.env
    volumes:
      - ./backend/jobs:/app/jobs

  frontend:
    build: ./frontend
    ports: ["5173:5173"]
    depends_on: [backend]
```

---

## 🛠️ Production Considerations

For production deployment, consider these upgrades:

| Concern | Current | Production |
|---|---|---|
| **Job storage** | In-memory dict | Redis / PostgreSQL |
| **File storage** | Local `jobs/` dir | S3 / Azure Blob |
| **Background tasks** | FastAPI BackgroundTasks | Celery / RQ |
| **Frontend hosting** | Vite dev server | Vercel / Netlify / Cloudflare Pages |
| **Backend hosting** | uvicorn local | Cloud Run / Railway / Render |
| **Authentication** | None | Auth0 / Clerk / NextAuth |
| **Rate limiting** | None | Add `slowapi` middleware |
| **CORS** | localhost only | Whitelist your prod domain |
| **Logging** | print() | structured logging (loguru) |
| **Monitoring** | None | Sentry + Prometheus |

---

## ❓ Troubleshooting

### Frontend can't reach backend
- Check backend is running on port 8000: `curl http://localhost:8000/api/health`
- Vite proxy is configured to `http://localhost:8000` — adjust in `vite.config.js` if different

### "ImportError: Could not import extraction modules"
- Copy your `extract_options.py` and `json_to_excel.py` to the `backend/` directory
- Or the app will run in mock mode (UI only, no real extraction)

### "ANTHROPIC_API_KEY not set"
- Edit `backend/.env` with your real API keys
- Restart the backend

### Browser shows nothing
- Open browser console (F12) and check for errors
- Make sure both backend (8000) and frontend (5173) are running
- Try a different browser if issues persist

---

## 📊 What Gets Shown in Results

The Results screen intelligently shows ONLY fields that contain data:

| If JSON has... | UI shows... |
|---|---|
| `company_name` | Company badge + header |
| `closing_balance` | KPI card + roll-forward row |
| `granted` (any plan) | "GRANTED" column in roll-forward table |
| `exercised` (any plan) | "EXERCISED" column |
| `vested` (any plan) | "VESTED" column |
| `forfeited_or_lapsed` | "FORFEITED" column |
| `weighted_avg_exercise_price` | "Avg exercise price" metric |
| `weighted_avg_grant_date_fair_value` | "Grant date FV" metric |
| `tranches[]` | Tranches detail table |
| `valuation_inputs.*` | Valuation inputs section |
| `prior_year.*` | Prior year row in tables |
| `_validation_summary.warnings[]` | "Items to verify" panel |

Fields that are `null` are completely **hidden** — no "—" placeholders, no empty columns.

---

## 🎯 Customization

### Change the brand color
Edit `frontend/tailwind.config.js`:
```js
colors: {
  brand: {
    DEFAULT: '#1F4E78',   // Change this
    light: '#2E75B6',
    pale: '#DDEBF7',
  },
}
```

### Change the favicon / logo
Replace the `<BarChart3>` icon in `App.jsx` with your own SVG or image.

### Add user authentication
- Frontend: Add a login screen before the upload screen
- Backend: Add JWT middleware or use OAuth (Auth0, Clerk)

### Persist jobs across server restarts
Replace the in-memory `JOBS` dict in `main.py` with a database (SQLAlchemy + SQLite is easiest).

---

## 📝 License

This is a starter template for your own project. Customize freely.

---

## 🤝 Need Help?

Check the API docs at http://localhost:8000/docs (auto-generated by FastAPI) for interactive API testing.
