# ============================================================
# CompeteGraph - PowerShell dev entry (equivalent to Makefile)
#
# Usage:
#   .\cg.ps1 init           # Init data/ skeleton
#   .\cg.ps1 install        # Install deps
#   .\cg.ps1 dev            # Run backend + frontend
#   .\cg.ps1 dev-backend
#   .\cg.ps1 dev-frontend
#   .\cg.ps1 test
#   .\cg.ps1 lint
#   .\cg.ps1 clean
#   .\cg.ps1 doctor
#   .\cg.ps1 help
#
# If "running scripts is disabled" error, run once:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#
# NOTE: All messages are in English to avoid Windows PowerShell 5
# (GBK) parsing issues. Chinese comments are fine after '#'.
# ============================================================

param(
    [Parameter(Position = 0)]
    [string]$Command = "help"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# Force UTF-8 output so child commands' output is readable
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    chcp 65001 > $null 2>&1
} catch {}

function Write-Step($msg) { Write-Host ">>> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Err($msg)  { Write-Host "[ERR] $msg" -ForegroundColor Red }

function Assert-Command($cmd, $hint) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Err "Command not found: $cmd"
        Write-Host "  $hint" -ForegroundColor Yellow
        exit 1
    }
}

# ============================================================
# Sub-commands
# ============================================================

function Invoke-Help {
    @"

CompeteGraph - dev commands

  .\cg.ps1 doctor         Check local dev env (python/uv/node/pnpm)
  .\cg.ps1 init           Init data/ skeleton
  .\cg.ps1 install        Install backend (uv) + frontend (pnpm) deps
  .\cg.ps1 dev            Start backend + frontend in two new windows
  .\cg.ps1 dev-backend    Backend only (http://localhost:8000)
  .\cg.ps1 dev-frontend   Frontend only (http://localhost:5173)
  .\cg.ps1 test           Run pytest + vitest
  .\cg.ps1 lint           Ruff + mypy + eslint
  .\cg.ps1 clean          Clean caches & build outputs
  .\cg.ps1 help           This message

"@ | Write-Host
}

function Invoke-Doctor {
    Write-Step "Checking dev environment..."
    $ok = $true

    # Python 3.11+
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $pyver = ((& python --version 2>&1) | Out-String).Trim()
        Write-Host "  python : $pyver"
        if ($pyver -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
                Write-Err "  -> Need Python 3.11+, got $pyver"
                $ok = $false
            }
        }
    } else {
        Write-Err "  python : NOT FOUND  -> https://www.python.org/downloads/"
        $ok = $false
    }

    # uv
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        $uvver = ((& uv --version 2>&1) | Out-String).Trim()
        Write-Host "  uv     : $uvver"
    } else {
        Write-Err "  uv     : NOT FOUND"
        Write-Host '    install: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"' -ForegroundColor Yellow
        $ok = $false
    }

    # Node 20+
    if (Get-Command node -ErrorAction SilentlyContinue) {
        $nodever = ((& node --version 2>&1) | Out-String).Trim()
        Write-Host "  node   : $nodever"
        if ($nodever -match "v(\d+)\.") {
            $major = [int]$Matches[1]
            if ($major -lt 20) {
                Write-Err "  -> Need Node 20+, got $nodever"
                $ok = $false
            }
        }
    } else {
        Write-Err "  node   : NOT FOUND  -> https://nodejs.org/  (install LTS 20+)"
        $ok = $false
    }

    # pnpm
    if (Get-Command pnpm -ErrorAction SilentlyContinue) {
        $pnpmver = ((& pnpm --version 2>&1) | Out-String).Trim()
        Write-Host "  pnpm   : $pnpmver"
    } else {
        Write-Err "  pnpm   : NOT FOUND  -> npm i -g pnpm"
        $ok = $false
    }

    # .env
    $envFile = Join-Path $RepoRoot "backend\.env"
    if (Test-Path $envFile) {
        Write-Host "  .env   : present"
    } else {
        Write-Host "  .env   : missing (later: copy backend\.env.example backend\.env)" -ForegroundColor Yellow
    }

    Write-Host ""
    if ($ok) {
        Write-Ok "Environment ready. Next: .\cg.ps1 install"
    } else {
        Write-Err "Fix missing items, then run .\cg.ps1 doctor again"
        exit 1
    }
}

function Invoke-Init {
    Write-Step "Init data/ directory..."
    & python (Join-Path $RepoRoot "scripts\init_data.py")
    if ($LASTEXITCODE -ne 0) {
        Write-Err "init_data.py failed"
        exit $LASTEXITCODE
    }
    Write-Ok "Done"
}

function Invoke-Install {
    Assert-Command "uv"   'install: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Assert-Command "pnpm" 'install: npm i -g pnpm'

    Write-Step "Backend: uv sync ..."
    Push-Location (Join-Path $RepoRoot "backend")
    try {
        & uv sync
        if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
    } finally {
        Pop-Location
    }

    Write-Step "Frontend: pnpm install ..."
    Push-Location (Join-Path $RepoRoot "frontend")
    try {
        & pnpm install
        if ($LASTEXITCODE -ne 0) { throw "pnpm install failed" }
    } finally {
        Pop-Location
    }

    Write-Ok "Dependencies installed"
}

function Invoke-DevBackend {
    Assert-Command "uv" "uv not installed"
    Write-Step "Backend at http://localhost:8000  (Ctrl+C to stop)"
    Push-Location (Join-Path $RepoRoot "backend")
    try {
        & uv run uvicorn cg.main:app --reload --host 0.0.0.0 --port 8000
    } finally {
        Pop-Location
    }
}

function Invoke-DevFrontend {
    Assert-Command "pnpm" "pnpm not installed"
    Write-Step "Frontend at http://localhost:5173  (Ctrl+C to stop)"
    Push-Location (Join-Path $RepoRoot "frontend")
    try {
        & pnpm dev
    } finally {
        Pop-Location
    }
}

function Invoke-Dev {
    Assert-Command "uv"   "uv not installed"
    Assert-Command "pnpm" "pnpm not installed"

    Write-Step "Starting backend + frontend in two new PowerShell windows"

    $backendCmd  = "cd '$RepoRoot\backend'; uv run uvicorn cg.main:app --reload --host 0.0.0.0 --port 8000"
    $frontendCmd = "cd '$RepoRoot\frontend'; pnpm dev"

    Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd
    Start-Sleep -Seconds 1
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCmd

    Write-Ok "Two windows opened. Close them to stop."
    Write-Host ""
    Write-Host "  Backend  : http://localhost:8000   (API + /docs)" -ForegroundColor Green
    Write-Host "  Frontend : http://localhost:5173" -ForegroundColor Green
    Write-Host ""
}

function Invoke-Test {
    Write-Step "Backend tests (pytest)..."
    Push-Location (Join-Path $RepoRoot "backend")
    try {
        & uv run pytest -v
        if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
    } finally {
        Pop-Location
    }

    Write-Step "Frontend tests (vitest)..."
    Push-Location (Join-Path $RepoRoot "frontend")
    try {
        & pnpm test
        if ($LASTEXITCODE -ne 0) { throw "vitest failed" }
    } finally {
        Pop-Location
    }

    Write-Ok "Tests passed"
}

function Invoke-Lint {
    Write-Step "Backend lint (ruff + mypy)..."
    Push-Location (Join-Path $RepoRoot "backend")
    try {
        & uv run ruff check .
        & uv run mypy cg
    } finally {
        Pop-Location
    }

    Write-Step "Frontend lint (eslint)..."
    Push-Location (Join-Path $RepoRoot "frontend")
    try {
        & pnpm lint
    } finally {
        Pop-Location
    }

    Write-Ok "Lint passed"
}

function Invoke-Clean {
    Write-Step "Cleaning caches and build outputs..."

    $patterns = @(
        "backend\.pytest_cache",
        "backend\.mypy_cache",
        "backend\.ruff_cache",
        "frontend\node_modules\.vite",
        "frontend\dist"
    )
    foreach ($p in $patterns) {
        $full = Join-Path $RepoRoot $p
        if (Test-Path $full) {
            Remove-Item -Recurse -Force $full
            Write-Host "  removed: $p"
        }
    }

    Get-ChildItem -Path $RepoRoot -Recurse -Force -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Remove-Item -Recurse -Force $_.FullName
            Write-Host "  removed: $($_.FullName.Substring($RepoRoot.Length + 1))"
        }

    Write-Ok "Clean done"
}

# ============================================================
# Dispatch
# ============================================================

switch ($Command.ToLower()) {
    "help"         { Invoke-Help }
    "-h"           { Invoke-Help }
    "--help"       { Invoke-Help }
    "doctor"       { Invoke-Doctor }
    "init"         { Invoke-Init }
    "install"      { Invoke-Install }
    "dev"          { Invoke-Dev }
    "dev-backend"  { Invoke-DevBackend }
    "dev-frontend" { Invoke-DevFrontend }
    "test"         { Invoke-Test }
    "lint"         { Invoke-Lint }
    "clean"        { Invoke-Clean }
    default {
        Write-Err "Unknown command: $Command"
        Invoke-Help
        exit 1
    }
}
