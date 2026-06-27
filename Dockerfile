# ════════════════════════════════════════════════════════════════════
# Options Extractor — backend-only container for Railway.
# FastAPI + Playwright Chromium (needed by the HTML->PDF market fetchers).
# Python is pinned here (3.12), so requirements.txt stays platform-neutral.
# ════════════════════════════════════════════════════════════════════
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Python dependencies (own layer so Docker caches it across code changes)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Chromium + its OS libraries — required by the fetchers that render
# ESEF/iXBRL reports to PDF (EU, Korea, Indonesia, EDGAR, ...).
RUN playwright install --with-deps chromium

# Application code (frontend excluded via .dockerignore -> API-only)
COPY . .

# Railway injects $PORT at runtime; default to 8000 for local `docker run`.
EXPOSE 8000
CMD ["sh", "-c", "uvicorn backend:app --host 0.0.0.0 --port ${PORT:-8000}"]
