@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   Delegatron Local Setup - Windows
echo   StoriaBrevi LLC
echo ============================================
echo.

:: Check Python version
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set PYMAJOR=%%a
    set PYMINOR=%%b
)

if %PYMAJOR% LSS 3 (
    echo [ERROR] Python 3.11+ required. Found Python %PYVER%
    pause
    exit /b 1
)
if %PYMAJOR%==3 if %PYMINOR% LSS 11 (
    echo [ERROR] Python 3.11+ required. Found Python %PYVER%
    pause
    exit /b 1
)
echo [OK] Python %PYVER%

:: Check Git
git --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git not found. Install from https://git-scm.com/download/win
    pause
    exit /b 1
)
echo [OK] Git found

:: Check Docker
docker info >nul 2>&1
if errorlevel 1 goto :no_docker
echo [OK] Docker found
goto :docker_done

:no_docker
echo [WARN] Docker not found or not running.
echo        Delegatron needs Docker to run code in a sandbox.
echo        You can use E2B sandbox instead: set DELEGATRON_SANDBOX=e2b
echo.
set /p INSTALL_DOCKER="Install Docker Desktop now? (Y/N): "
if /i not "!INSTALL_DOCKER!"=="Y" (
    echo Skipping Docker install. You can install it later from:
    echo https://www.docker.com/products/docker-desktop/
    echo.
    goto :docker_done
)
echo Downloading Docker Desktop installer...
curl -L -o "%TEMP%\DockerDesktopInstaller.exe" "https://desktop.docker.com/win/main/amd64/Docker%%20Desktop%%20Installer.exe"
if errorlevel 1 (
    echo [ERROR] Download failed. Install manually: https://www.docker.com/products/docker-desktop/
    goto :docker_done
)
echo Running Docker Desktop installer (requires admin privileges)...
echo You may see a UAC prompt - click Yes to allow.
start /wait "" "%TEMP%\DockerDesktopInstaller.exe" install --quiet --accept-license
if errorlevel 1 (
    echo [WARN] Docker installer exited with errors.
    echo        You may need to restart and run Docker Desktop manually.
) else (
    echo [OK] Docker Desktop installed
    echo [NOTE] You will need to restart your computer before Docker is available.
)
del "%TEMP%\DockerDesktopInstaller.exe" >nul 2>&1

:docker_done

:: Create virtual environment
if exist .venv (
    echo [OK] Virtual environment already exists
) else (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created
)

:: Activate venv
call .venv\Scripts\activate.bat

:: Upgrade pip
echo Upgrading pip...
python -m pip install --upgrade pip --quiet

:: Pre-install libcst (requires Rust on some platforms; pre-built wheel avoids this)
echo Installing build prerequisites...
pip install libcst --quiet 2>nul
if errorlevel 1 (
    echo [WARN] libcst pre-install failed. If the main install fails,
    echo        you may need to install Rust: https://rustup.rs
)

:: Install Delegatron and dependencies
echo Installing Delegatron and dependencies (this may take several minutes)...
pip install -e ".[dev,cloud-providers]" --quiet
if errorlevel 1 (
    echo [ERROR] Installation failed. Check the output above for details.
    echo.
    echo Common fixes:
    echo   - If libcst fails: install Rust from https://rustup.rs
    echo   - Then retry: pip install -e ".[dev,cloud-providers]"
    pause
    exit /b 1
)
echo [OK] Delegatron installed

:: Create .env file if it doesn't exist
if not exist .env (
    echo Creating .env file...
    > .env echo # Delegatron Environment Configuration
    >> .env echo #
    >> .env echo # LOCAL MODELS - no API keys needed!
    >> .env echo # Just run: delegatron run --preset local "your task"
    >> .env echo #
    >> .env echo # CLOUD MODELS - uncomment the keys you want to use:
    >> .env echo # ANTHROPIC_API_KEY=sk-ant-...
    >> .env echo # OPENAI_API_KEY=sk-...
    >> .env echo # GEMINI_API_KEY=...
    >> .env echo.
    >> .env echo # Pipeline settings
    >> .env echo # DELEGATRON_MAX_ITERATIONS=5
    >> .env echo # DELEGATRON_MAX_COST_USD=5.00
    >> .env echo # DELEGATRON_SANDBOX=docker
    >> .env echo # DELEGATRON_LOG_LEVEL=INFO
    echo [OK] Created .env template
) else (
    echo [OK] .env already exists
)

:: Verify installation
echo.
echo Verifying installation...
delegatron --version >nul 2>&1
if errorlevel 1 (
    python -m delegatron --version >nul 2>&1
    if errorlevel 1 (
        echo [WARN] CLI not on PATH. Use: python -m delegatron
    ) else (
        echo [OK] CLI available via: python -m delegatron
    )
) else (
    for /f "tokens=*" %%v in ('delegatron --version 2^>^&1') do echo [OK] %%v
)

:: Done
echo.
echo ============================================
echo   Setup complete!
echo ============================================
echo.
echo Next steps:
echo.
echo   Option A - Local models (free, no API keys):
echo     1. Install Ollama: https://ollama.com/download
echo     2. ollama pull phi4:14b  and  ollama pull qwen2.5-coder:14b
echo     3. delegatron run --preset local "Add a hello world endpoint"
echo.
echo   Option B - Cloud models:
echo     1. Edit .env and add an API key
echo     2. delegatron run "Add a hello world endpoint"
echo.
echo   Dashboard:
echo     delegatron serve
echo.
echo The virtual environment is now active. To reactivate later:
echo   .venv\Scripts\activate
echo.
echo Docs: docs\install.md and docs\deployment.md
echo.

:: Keep the venv active in the current shell by dropping into cmd
:: endlocal preserves the activated venv PATH for the spawned shell
endlocal & call .venv\Scripts\activate.bat & cmd /k "echo Delegatron venv active. Type 'deactivate' to exit."
