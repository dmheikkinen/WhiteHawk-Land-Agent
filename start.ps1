# Ohio Landman Pipeline -- Windows quick-start script
#
# FIRST-TIME ONLY: unlock PowerShell scripts
#   Windows blocks .ps1 files by default.  Run this ONCE, then re-run normally:
#
#     Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
#
#   Or bypass for a single run (no permanent change):
#
#     powershell -ExecutionPolicy Bypass -File .\start.ps1
#
# Normal usage:
#   .\start.ps1
#   .\start.ps1 -Port 8080
#
# What it does:
#   1. Creates a .venv virtual environment if it doesn't exist
#   2. Installs / upgrades all dependencies from requirements.txt
#   3. Scaffolds a .env file from .env.example on first run, then exits
#   4. Loads .env into the current session environment
#   5. Checks for required API keys and warns if missing
#   6. Starts the web UI at http://127.0.0.1:<Port>

param(
    [string]$Port = "5000"
)

$ErrorActionPreference = "Stop"

function Write-Step { param([string]$msg) Write-Host "  >> $msg" -ForegroundColor Cyan }
function Write-Ok   { param([string]$msg) Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn { param([string]$msg) Write-Host "  [!!] $msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "  Ohio Landman Pipeline" -ForegroundColor White
Write-Host "  -----------------------------------------" -ForegroundColor DarkGray
Write-Host ""

# -- 1) Virtual environment ---------------------------------------------------
if (-not (Test-Path ".venv")) {
    Write-Step "Creating virtual environment (.venv)..."
    python -m venv .venv
    Write-Ok "Virtual environment created"
} else {
    Write-Ok "Virtual environment already exists"
}

# -- 2) Dependencies ----------------------------------------------------------
Write-Step "Installing / verifying dependencies..."
& .venv\Scripts\pip install --upgrade pip --quiet
& .venv\Scripts\pip install -r requirements.txt --quiet
Write-Ok "Dependencies up to date"

# -- 3) Scaffold .env ---------------------------------------------------------
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Warn ".env created from .env.example"
    Write-Host "    Open .env in a text editor, fill in your API keys, then re-run:" -ForegroundColor Yellow
    Write-Host "      .\start.ps1" -ForegroundColor White
    Write-Host ""
    exit 0
} else {
    Write-Ok ".env found"
}

# -- 4) Load .env into this session -------------------------------------------
Write-Step "Loading environment variables from .env..."
Get-Content ".env" |
    Where-Object { $_ -notmatch '^\s*#' -and $_ -match '\S' -and $_ -match '=' } |
    ForEach-Object {
        $parts = $_ -split '=', 2
        $key   = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        if ($key) {
            [System.Environment]::SetEnvironmentVariable($key, $value, 'Process')
        }
    }
Write-Ok "Environment variables loaded"

# -- 5) Quick key check -------------------------------------------------------
$missingKeys = @()
if (-not $env:OPENAI_API_KEY)  { $missingKeys += "OPENAI_API_KEY" }
if (-not $env:SERPAPI_API_KEY) { $missingKeys += "SERPAPI_API_KEY" }
if ($missingKeys.Count -gt 0) {
    Write-Host ""
    Write-Warn "The following keys are not set in .env:"
    $missingKeys | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
    Write-Host "    Some pipeline phases will be disabled until these are filled in." -ForegroundColor DarkGray
    Write-Host ""
}

# -- 6) Launch the web UI -----------------------------------------------------
Write-Host ""
Write-Host "  Starting web UI..." -ForegroundColor White
Write-Host "  Open http://127.0.0.1:$Port in your browser" -ForegroundColor Green
Write-Host "  Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

& .venv\Scripts\uvicorn server:app --host 127.0.0.1 --port $Port --reload
