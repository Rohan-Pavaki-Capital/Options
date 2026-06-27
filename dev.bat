@echo off
REM Single-command dev launcher: starts FastAPI backend + Vite frontend
REM in two separate console windows.
REM
REM Usage:  dev.bat
REM
REM Open the frontend at  http://localhost:5173
REM Open API docs at      http://localhost:8000/docs

echo Starting FastAPI backend on http://localhost:8000 ...
start "Options Extractor - Backend" cmd /k "uvicorn backend:app --reload --port 8000"

echo Starting Vite frontend on http://localhost:5173 ...
start "Options Extractor - Frontend" cmd /k "cd Frontend && npm install && npm run dev"

echo.
echo Both services launched in separate windows.
echo   Backend : http://localhost:8000
echo   Frontend: http://localhost:5173
