$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $projectDir ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$installMarker = Join-Path $venvDir ".dependencies-v2.1.0"
$envFile = Join-Path $projectDir ".env"
$envExample = Join-Path $projectDir ".env.example"

Set-Location -LiteralPath $projectDir

if (-not (Test-Path -LiteralPath $pythonExe)) {
    Write-Host "[1/4] Creating Python virtual environment..." -ForegroundColor Cyan
    python -m venv $venvDir
}

if (-not (Test-Path -LiteralPath $installMarker)) {
    Write-Host "[2/4] Installing project dependencies. This may take a few minutes..." -ForegroundColor Cyan
    & $pythonExe -m pip install --upgrade pip
    & $pythonExe -m pip install -e "${projectDir}[dev]"
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed. Check the network connection and run this file again."
    }
    New-Item -ItemType File -Path $installMarker -Force | Out-Null
} else {
    Write-Host "[2/4] Dependencies already installed." -ForegroundColor Green
}

if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath $envExample -Destination $envFile
    Write-Host "[3/4] Created .env. Fill in MODEL_API_KEY, then start again." -ForegroundColor Yellow
    notepad.exe $envFile
    Read-Host "After saving .env, press Enter to continue (or close this window and start again)"
} else {
    Write-Host "[3/4] Found .env configuration." -ForegroundColor Green
}

Write-Host "[4/4] Starting http://127.0.0.1:8000" -ForegroundColor Cyan
Write-Host "To stop the service, press Ctrl+C in this window." -ForegroundColor DarkGray
& $pythonExe run_local.py
