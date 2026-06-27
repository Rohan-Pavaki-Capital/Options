@echo off
REM ============================================================
REM  Share the Options Extractor app via a free Cloudflare link
REM ============================================================
REM  1. Starts the FastAPI backend (also serves the React UI)
REM     on http://localhost:8000
REM  2. Opens a free Cloudflare Tunnel and prints a public
REM     https://*.trycloudflare.com link anyone can open.
REM
REM  Keep this window open while sharing. Close it to stop.
REM ============================================================

cd /d "%~dp0"

echo.
echo [1/2] Starting backend on http://localhost:8000 ...
start "Options Extractor Backend" cmd /c "python -m uvicorn backend:app --host 0.0.0.0 --port 8000"

echo     Waiting for the backend to come up ...
timeout /t 6 /nobreak >nul

echo.
echo [2/2] Opening the public Cloudflare link below.
echo     Look for the https://xxxx.trycloudflare.com URL and share it.
echo.
cloudflared.exe tunnel --url http://localhost:8000
