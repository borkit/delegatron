@echo off
setlocal

cd /d %~dp0

if not exist .venv\Scripts\activate.bat (
    echo [ERROR] Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

REM Build dashboard if not already built
if not exist dashboard\.next\BUILD_ID (
    echo Building dashboard...
    cd dashboard
    call npm run build
    cd ..
    if not exist dashboard\.next\BUILD_ID (
        echo [ERROR] Dashboard build failed.
        pause
        exit /b 1
    )
    echo [OK] Dashboard built
)

REM Kill any stale *Python* processes on our ports (preserve svchost etc.)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000.*LISTENING" 2^>nul') do (
    tasklist /FI "PID eq %%p" 2>nul | findstr /I "python.exe" >nul && taskkill /f /pid %%p >nul 2>&1
)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8001.*LISTENING" 2^>nul') do (
    tasklist /FI "PID eq %%p" 2>nul | findstr /I "node.exe" >nul && taskkill /f /pid %%p >nul 2>&1
)

REM Clear Next.js per-route page cache so stale prerenders from a previous
REM run (generated while the API server was down) don't linger.
if exist dashboard\.next\cache (
    rmdir /s /q dashboard\.next\cache >nul 2>&1
)

echo.
echo TIP: If the dashboard shows "API error 404", press Ctrl+Shift+R in the
echo      browser to bypass cache. If that does not fix it, open Settings on
echo      the dashboard and reset the API URL (or clear delegatron_api_url in
echo      browser localStorage).
echo.

delegatron serve

endlocal
