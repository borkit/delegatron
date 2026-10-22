@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   Delegatron Cleanup
echo   StoriaBrevi LLC
echo ============================================
echo.

cd /d %~dp0

REM Only warn if a *Python* process is on port 8000 — other listeners
REM (e.g. Windows IP Helper svchost for Teredo tunneling) are harmless.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000.*LISTENING" 2^>nul') do (
    tasklist /FI "PID eq %%p" 2>nul | findstr /I "python.exe" >nul
    if !errorlevel! equ 0 (
        echo [WARN] Delegatron server (PID %%p^) is running on port 8000. Stop it first.
        echo        Press Ctrl+C in the server window, then run cleanup again.
        pause
        exit /b 1
    )
)

REM ---- Log files ----
set LOG_SIZE=0
if exist logs (
    for %%f in (logs\*) do set /a LOG_SIZE+=%%~zf
)
if !LOG_SIZE! GTR 0 (
    echo [1] Log files            logs\
    for %%f in (logs\*) do echo       %%f  ^(%%~zf bytes^)
    set /p CLEAN_LOGS="    Delete log files? (Y/N): "
    if /i "!CLEAN_LOGS!"=="Y" (
        del /q logs\* 2>nul
        echo     [OK] Logs cleared
    ) else (
        echo     [--] Skipped
    )
    echo.
) else (
    echo [1] Log files            (none found)
    echo.
)

REM ---- Run artifacts ----
set RUN_COUNT=0
if exist runs (
    for /d %%d in (runs\*) do set /a RUN_COUNT+=1
)
if !RUN_COUNT! GTR 0 (
    echo [2] Run artifacts        runs\  ^(!RUN_COUNT! runs^)
    for /d %%d in (runs\*) do echo       %%~nxd
    set /p CLEAN_RUNS="    Delete all run artifacts? (Y/N): "
    if /i "!CLEAN_RUNS!"=="Y" (
        rmdir /s /q runs 2>nul
        mkdir runs 2>nul
        echo     [OK] Runs cleared
    ) else (
        echo     [--] Skipped
    )
    echo.
) else (
    echo [2] Run artifacts        (none found)
    echo.
)

REM ---- Run workspaces ----
set WS_COUNT=0
if exist .delegatron-runs (
    for /d %%d in (.delegatron-runs\*) do set /a WS_COUNT+=1
)
if !WS_COUNT! GTR 0 (
    echo [3] Run workspaces       .delegatron-runs\  ^(!WS_COUNT! workspaces^)
    for /d %%d in (.delegatron-runs\*) do echo       %%~nxd
    set /p CLEAN_WS="    Delete run workspaces? (Y/N): "
    if /i "!CLEAN_WS!"=="Y" (
        rmdir /s /q .delegatron-runs 2>nul
        echo     [OK] Run workspaces cleared
    ) else (
        echo     [--] Skipped
    )
    echo.
) else (
    echo [3] Run workspaces       (none found)
    echo.
)

REM ---- Git run branches ----
set BRANCH_COUNT=0
for /f "tokens=*" %%b in ('git branch --list "delegatron/run-*" 2^>nul') do set /a BRANCH_COUNT+=1
if !BRANCH_COUNT! GTR 0 (
    echo [4] Git run branches     ^(!BRANCH_COUNT! branches^)
    for /f "tokens=*" %%b in ('git branch --list "delegatron/run-*" 2^>nul') do echo      %%b
    set /p CLEAN_BRANCHES="    Delete stale run branches? (Y/N): "
    if /i "!CLEAN_BRANCHES!"=="Y" (
        for /f "tokens=*" %%b in ('git branch --list "delegatron/run-*" 2^>nul') do (
            git branch -D %%b >nul 2>&1
        )
        echo     [OK] Run branches deleted
    ) else (
        echo     [--] Skipped
    )
    echo.
) else (
    echo [4] Git run branches     (none found)
    echo.
)

REM ---- Python caches ----
set CACHE_COUNT=0
for /d /r src %%d in (__pycache__) do if exist "%%d" set /a CACHE_COUNT+=1
for /d /r tests %%d in (__pycache__) do if exist "%%d" set /a CACHE_COUNT+=1
if !CACHE_COUNT! GTR 0 (
    echo [5] Python caches        ^(!CACHE_COUNT! __pycache__ dirs^)
    set /p CLEAN_CACHE="    Delete Python caches? (Y/N): "
    if /i "!CLEAN_CACHE!"=="Y" (
        for /d /r src %%d in (__pycache__) do if exist "%%d" rmdir /s /q "%%d" 2>nul
        for /d /r tests %%d in (__pycache__) do if exist "%%d" rmdir /s /q "%%d" 2>nul
        del /s /q *.pyc 2>nul >nul
        echo     [OK] Python caches cleared
    ) else (
        echo     [--] Skipped
    )
    echo.
) else (
    echo [5] Python caches        (none found)
    echo.
)

REM ---- Dashboard build cache ----
set DASH_CACHE=0
if exist dashboard\.next (
    set DASH_CACHE=1
)
if !DASH_CACHE! GTR 0 (
    echo [6] Dashboard cache      dashboard\.next\
    set /p CLEAN_DASH="    Delete dashboard build cache? (Y/N): "
    if /i "!CLEAN_DASH!"=="Y" (
        rmdir /s /q dashboard\.next 2>nul
        echo     [OK] Dashboard cache cleared
    ) else (
        echo     [--] Skipped
    )
    echo.
) else (
    echo [6] Dashboard cache      (none found)
    echo.
)

REM ---- SQLite database ----
if exist data\delegatron.db (
    echo [7] Database             data\delegatron.db
    echo     WARNING: This contains API keys and team data.
    set /p CLEAN_DB="    Delete database? (Y/N): "
    if /i "!CLEAN_DB!"=="Y" (
        del /q data\delegatron.db 2>nul
        echo     [OK] Database deleted (will be recreated on next start)
    ) else (
        echo     [--] Skipped
    )
    echo.
) else (
    echo [7] Database             (none found)
    echo.
)

REM ---- Docker images ----
echo [8] Docker images
docker images --format "{{.Repository}}:{{.Tag}}  {{.Size}}" --filter "reference=mcr.microsoft.com/dotnet/*" --filter "reference=python:*" 2>nul
set /p CLEAN_DOCKER="    Prune unused Docker images? (Y/N): "
if /i "!CLEAN_DOCKER!"=="Y" (
    docker image prune -f >nul 2>&1
    echo     [OK] Unused Docker images pruned
) else (
    echo     [--] Skipped
)
echo.

REM ---- Dashboard rebuild ----
if exist dashboard\out (
    echo [9] Dashboard static build
    set /p CLEAN_DASHBOARD_BUILD="    Rebuild dashboard? Fixes stale pages and caching issues (Y/N): "
    if /i "!CLEAN_DASHBOARD_BUILD!"=="Y" (
        rmdir /s /q dashboard\out 2>nul
        cd dashboard
        call npm run build >nul 2>&1
        cd ..
        if exist dashboard\out\index.html (
            echo     [OK] Dashboard rebuilt
        ) else (
            echo     [WARN] Rebuild failed. Run manually: cd dashboard ^&^& npm run build
        )
    ) else (
        echo     [--] Skipped
    )
    echo.
) else (
    echo [9] Dashboard static build  (not built yet)
    echo.
)

echo ============================================
echo   Cleanup complete
echo ============================================
echo.
echo   TIP: If dashboard pages don't load, press Ctrl+Shift+R
echo        in the browser to hard-refresh.
echo.
pause

endlocal
