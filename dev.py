"""
Single-command dev launcher (cross-platform).

Spawns the FastAPI backend (uvicorn) and the Vite frontend (npm run dev)
as child processes, streams their interleaved logs to this console, and
shuts both down on Ctrl-C.

Usage:
    python dev.py

URLs:
    Frontend (open this): http://localhost:5173
    Backend API docs:     http://localhost:8000/docs
"""
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
FRONTEND_DIR = ROOT / "Frontend"

IS_WINDOWS = os.name == "nt"


def spawn(cmd, cwd, label):
    print(f"[dev] starting {label}: {' '.join(cmd)}  (cwd={cwd})")
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        shell=IS_WINDOWS,  # Needed on Windows so 'npm' resolves
    )


def main():
    if not FRONTEND_DIR.exists():
        sys.exit("ERROR: Frontend/ directory not found.")

    vite_bin = FRONTEND_DIR / "node_modules" / ".bin" / ("vite.cmd" if IS_WINDOWS else "vite")
    if not vite_bin.exists():
        print("[dev] frontend deps missing — running 'npm install' (one-time) ...")
        subprocess.run(["npm", "install"], cwd=str(FRONTEND_DIR), shell=IS_WINDOWS, check=True)

    backend = spawn(
        ["uvicorn", "backend:app", "--reload", "--port", "8000"],
        cwd=ROOT,
        label="backend (uvicorn)",
    )
    frontend = spawn(
        ["npm", "run", "dev"],
        cwd=FRONTEND_DIR,
        label="frontend (vite)",
    )

    print("\n[dev] both services launched.")
    print("[dev]   Frontend: http://localhost:5173")
    print("[dev]   Backend : http://localhost:8000/docs\n")

    procs = [backend, frontend]

    def shutdown(*_):
        print("\n[dev] shutting down ...")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, shutdown)

    try:
        while True:
            for p in procs:
                rc = p.poll()
                if rc is not None:
                    print(f"[dev] child exited (rc={rc}) — stopping siblings.")
                    shutdown()
            try:
                backend.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
