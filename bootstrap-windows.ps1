# Delegatron Windows Bootstrap — Zero to Running
#
# Run this on a CLEAN Windows 10/11 machine with nothing installed.
# It installs all prerequisites, clones the repo, and sets up Delegatron.
#
# HOW TO RUN (copy-paste into PowerShell as Administrator):
#
#   Set-ExecutionPolicy Bypass -Scope Process -Force
#   iwr -useb https://raw.githubusercontent.com/borkit/delegatron/main/bootstrap-windows.ps1 | iex
#
# OR save this file and run:
#   powershell -ExecutionPolicy Bypass -File bootstrap-windows.ps1
#

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Delegatron Windows Bootstrap" -ForegroundColor Cyan
Write-Host "  Zero to Running — Clean Install" -ForegroundColor Cyan
Write-Host "  StoriaBrevi LLC" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# --- Helper ---
function Test-Command($cmd) {
    try { Get-Command $cmd -ErrorAction Stop | Out-Null; return $true }
    catch { return $false }
}

function Install-WithWinget($id, $name) {
    Write-Host "[...] Installing $name..." -ForegroundColor Yellow
    winget install --id $id --accept-source-agreements --accept-package-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[WARN] winget install for $name may have failed. Check above." -ForegroundColor Yellow
    } else {
        Write-Host "[OK] $name installed" -ForegroundColor Green
    }
}

# --- Step 0: Check winget ---
Write-Host "Step 0: Checking winget (Windows package manager)..." -ForegroundColor White
if (-not (Test-Command "winget")) {
    Write-Host "[INFO] winget not found. It ships with Windows 10 1809+ and Windows 11." -ForegroundColor Yellow
    Write-Host "       Installing App Installer from Microsoft Store..." -ForegroundColor Yellow
    try {
        Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe
        # Refresh PATH
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
    } catch {
        Write-Host "[ERROR] Could not install winget automatically." -ForegroundColor Red
        Write-Host "        Install 'App Installer' from Microsoft Store, then re-run this script." -ForegroundColor Red
        exit 1
    }
}
Write-Host "[OK] winget available" -ForegroundColor Green

# --- Step 1: Python ---
Write-Host ""
Write-Host "Step 1: Python 3.11+..." -ForegroundColor White
if (Test-Command "python") {
    $pyver = python --version 2>&1
    Write-Host "[OK] $pyver already installed" -ForegroundColor Green
} else {
    Install-WithWinget "Python.Python.3.12" "Python 3.12"
    # Refresh PATH so python is available
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
    if (-not (Test-Command "python")) {
        Write-Host "[ERROR] Python still not found after install. Restart PowerShell and re-run." -ForegroundColor Red
        exit 1
    }
    $pyver = python --version 2>&1
    Write-Host "[OK] $pyver" -ForegroundColor Green
}

# --- Step 2: Git ---
Write-Host ""
Write-Host "Step 2: Git..." -ForegroundColor White
if (Test-Command "git") {
    $gitver = git --version 2>&1
    Write-Host "[OK] $gitver already installed" -ForegroundColor Green
} else {
    Install-WithWinget "Git.Git" "Git"
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
    if (-not (Test-Command "git")) {
        Write-Host "[ERROR] Git still not found after install. Restart PowerShell and re-run." -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] Git installed" -ForegroundColor Green
}

# --- Step 3: Docker Desktop ---
Write-Host ""
Write-Host "Step 3: Docker Desktop (for sandbox)..." -ForegroundColor White
if (Test-Command "docker") {
    docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[OK] Docker is running" -ForegroundColor Green
    } else {
        Write-Host "[WARN] Docker is installed but not running. Start Docker Desktop." -ForegroundColor Yellow
    }
} else {
    $installDocker = Read-Host "Install Docker Desktop? (Y/N) [recommended]"
    if ($installDocker -eq "Y" -or $installDocker -eq "y") {
        Install-WithWinget "Docker.DockerDesktop" "Docker Desktop"
        Write-Host "[NOTE] Restart your PC after this script finishes to complete Docker setup." -ForegroundColor Yellow
    } else {
        Write-Host "[SKIP] Docker skipped. You can use mock mode or E2B sandbox instead." -ForegroundColor Yellow
    }
}

# --- Step 4: Ollama (local LLMs) ---
Write-Host ""
Write-Host "Step 4: Ollama (free local models)..." -ForegroundColor White
if (Test-Command "ollama") {
    Write-Host "[OK] Ollama already installed" -ForegroundColor Green
} else {
    $installOllama = Read-Host "Install Ollama for free local AI models? (Y/N) [recommended]"
    if ($installOllama -eq "Y" -or $installOllama -eq "y") {
        Install-WithWinget "Ollama.Ollama" "Ollama"
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
        Write-Host "[OK] Ollama installed" -ForegroundColor Green
    } else {
        Write-Host "[SKIP] Ollama skipped. You'll need cloud API keys instead." -ForegroundColor Yellow
    }
}

# --- Step 5: Node.js (for dashboard) ---
Write-Host ""
Write-Host "Step 5: Node.js (for web dashboard)..." -ForegroundColor White
if (Test-Command "node") {
    $nodever = node --version 2>&1
    Write-Host "[OK] Node.js $nodever already installed" -ForegroundColor Green
} else {
    $installNode = Read-Host "Install Node.js for the web dashboard? (Y/N) [optional]"
    if ($installNode -eq "Y" -or $installNode -eq "y") {
        Install-WithWinget "OpenJS.NodeJS.LTS" "Node.js LTS"
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
        Write-Host "[OK] Node.js installed" -ForegroundColor Green
    } else {
        Write-Host "[SKIP] Node.js skipped. CLI and API still work without it." -ForegroundColor Yellow
    }
}

# --- Step 6: Clone Delegatron ---
Write-Host ""
Write-Host "Step 6: Cloning Delegatron..." -ForegroundColor White

$installDir = "$HOME\delegatron"
if (Test-Path $installDir) {
    Write-Host "[OK] $installDir already exists" -ForegroundColor Green
} else {
    git clone https://github.com/borkit/delegatron.git $installDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to clone repository" -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] Cloned to $installDir" -ForegroundColor Green
}

Set-Location $installDir

# --- Step 7: Python venv + install ---
Write-Host ""
Write-Host "Step 7: Setting up Python environment..." -ForegroundColor White

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    Write-Host "[OK] Virtual environment created" -ForegroundColor Green
} else {
    Write-Host "[OK] Virtual environment already exists" -ForegroundColor Green
}

# Activate venv
& .\.venv\Scripts\Activate.ps1

# Install
Write-Host "[...] Installing Delegatron (this may take a few minutes)..." -ForegroundColor Yellow
pip install --upgrade pip --quiet
pip install -e ".[dev,cloud-providers]" --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Installation failed. Check output above." -ForegroundColor Red
    Write-Host "        Common fix: install Rust from https://rustup.rs then retry." -ForegroundColor Yellow
    exit 1
}

$dlgver = delegatron --version 2>&1
Write-Host "[OK] $dlgver installed" -ForegroundColor Green

# --- Step 8: Create .env ---
if (-not (Test-Path ".env")) {
    @"
# Delegatron Environment Configuration
#
# LOCAL MODELS - no API keys needed!
# Just run: delegatron run --preset local "your task"
#
# CLOUD MODELS - uncomment the keys you want to use:
# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# GEMINI_API_KEY=...

# Pipeline settings
# DELEGATRON_MAX_ITERATIONS=5
# DELEGATRON_MAX_COST_USD=5.00
# DELEGATRON_SANDBOX=docker
"@ | Out-File -FilePath ".env" -Encoding utf8
    Write-Host "[OK] Created .env template" -ForegroundColor Green
}

# --- Step 9: Pull Ollama model ---
if (Test-Command "ollama") {
    Write-Host ""
    Write-Host "Step 9: Pulling Ollama model..." -ForegroundColor White
    $pullModel = Read-Host "Pull phi4:14b and qwen2.5-coder:14b for the 'local' preset now? (~18GB, takes a while) (Y/N)"
    if ($pullModel -eq "Y" -or $pullModel -eq "y") {
        ollama pull phi4:14b
        ollama pull qwen2.5-coder:14b
        Write-Host "[OK] Models ready" -ForegroundColor Green
    }
}

# --- Done ---
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Delegatron is ready!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Quick test (no API keys, no Docker needed):" -ForegroundColor White
Write-Host "  delegatron run --preset mock `"Add a hello world endpoint`"" -ForegroundColor Cyan
Write-Host ""
if (Test-Command "ollama") {
    Write-Host "Run with local AI (free):" -ForegroundColor White
    Write-Host "  delegatron run --preset local `"Add a /health endpoint with tests`"" -ForegroundColor Cyan
    Write-Host ""
}
Write-Host "Web dashboard:" -ForegroundColor White
Write-Host "  start-dashboard.bat" -ForegroundColor Cyan
Write-Host ""
Write-Host "Code review:" -ForegroundColor White
Write-Host "  delegatron review --workspace C:\path\to\your\project" -ForegroundColor Cyan
Write-Host ""
Write-Host "Location: $installDir" -ForegroundColor Gray
Write-Host "Activate: .venv\Scripts\Activate.ps1" -ForegroundColor Gray
Write-Host "Docs:     docs\install.md" -ForegroundColor Gray
Write-Host ""
if (-not (Test-Command "docker")) {
    Write-Host "[REMINDER] Install Docker Desktop and restart your PC for sandbox support." -ForegroundColor Yellow
}
