@echo off
REM ============================================================
REM  Options Extractor - start backend + Cloudflare tunnel
REM  Double-click this file. It starts both services (in their
REM  own windows, which must stay open) and then shows you the
REM  public tunnel URL to paste into your VBA script.
REM ============================================================

set "ROOT=%~dp0"
cd /d "%ROOT%"

set OPENBLAS_NUM_THREADS=1
set OMP_NUM_THREADS=1
set PYTHONUTF8=1

echo Stopping any old instances...
taskkill /F /IM cloudflared.exe >nul 2>&1
for /f "tokens=2" %%p in ('tasklist /v /fi "imagename eq python.exe" /fo list ^| find "PID:"') do rem

echo Starting backend on http://localhost:8000 ...
start "Options Extractor - Backend" cmd /k ""%ROOT%.rog\Scripts\python.exe" -m uvicorn backend:app --host 0.0.0.0 --port 8000 ^>^> "%ROOT%backend.log" 2^>^&1"

REM fresh tunnel log so we read THIS run's URL
if exist "%ROOT%cf_tunnel.log" del "%ROOT%cf_tunnel.log"

echo Starting Cloudflare tunnel ...
start "Options Extractor - Tunnel" cmd /k ""%ROOT%cloudflared.exe" tunnel --url http://localhost:8000 ^> "%ROOT%cf_tunnel.log" 2^>^&1"

echo.
echo Waiting for the tunnel URL (up to ~30s)...

REM Poll the tunnel log for the trycloudflare URL, show it, copy to clipboard.
powershell -NoProfile -Command ^
  "$u=$null; for($i=0;$i -lt 30;$i++){ Start-Sleep 1; $m = Get-Content '%ROOT%cf_tunnel.log' -ErrorAction SilentlyContinue ^| Select-String 'https://[a-z0-9-]+\.trycloudflare\.com' ^| Select-Object -Last 1; if($m){ $u=([regex]'https://[a-z0-9-]+\.trycloudflare\.com').Match($m.Line).Value; break } }; if($u){ $full=$u + '/api/simply/grouped?ticker='; Set-Clipboard $full; Write-Host ''; Write-Host '==================================================='; Write-Host ' TUNNEL URL (copied to clipboard):'; Write-Host ''; Write-Host ('   ' + $u); Write-Host ''; Write-Host ' Paste this into your VBA:'; Write-Host ('   url = \"' + $full + '\" ^& ticker'); Write-Host '==================================================='; Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::ShowDialog('URL copied to clipboard:' + [char]10 + [char]10 + $u + '/api/simply/grouped?ticker=' + [char]10 + [char]10 + 'Paste into your VBA url line.') ^| Out-Null } else { Write-Host 'Could not read tunnel URL - open cf_tunnel.log manually.' }"

echo.
echo Keep the two service windows OPEN while you use the API.
pause
