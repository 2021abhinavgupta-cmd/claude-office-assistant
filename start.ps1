$ErrorActionPreference = "Stop"

# Claude Office Assistant - Windows startup script
# Run from PowerShell: .\start.ps1

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$envFile = Join-Path $Root "config/.env"
if (-not (Test-Path $envFile)) {
    Write-Host ""
    Write-Host "config/.env not found. Create it with:"
    Write-Host "  ANTHROPIC_API_KEY=sk-ant-api03-YOUR-KEY"
    Write-Host "  FLASK_SECRET_KEY=<64-char-hex>"
    Write-Host ""
    exit 1
}

if (Select-String -Path $envFile -Pattern "^ANTHROPIC_API_KEY=\s*$" -Quiet) {
    Write-Host ""
    Write-Host "ANTHROPIC_API_KEY is empty in config/.env."
    Write-Host "Set it before starting the app."
    Write-Host ""
}

$pythonCmd = "py"
try {
    & $pythonCmd -3 --version | Out-Null
} catch {
    Write-Host "Python launcher (py) not found. Install Python 3 and retry."
    exit 1
}

$venvPython = Join-Path $Root "venv/Scripts/python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment..."
    & $pythonCmd -3 -m venv venv
}

$venvPip = Join-Path $Root "venv/Scripts/pip.exe"
Write-Host "Installing Python dependencies..."
& $venvPip install -q -r "backend/requirements.txt"

$port = 5000
$pids = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
if ($pids) {
    Write-Host "Freeing port $port..."
    foreach ($procId in $pids) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

Write-Host ""
Write-Host "Claude Office Assistant"
Write-Host "Open: http://localhost:5000"
Write-Host "Dashboard: http://localhost:5000/dashboard.html"
Write-Host "Press Ctrl+C to stop"
Write-Host ""

Start-Process "http://localhost:5000" | Out-Null

Set-Location (Join-Path $Root "backend")
# Gunicorn depends on fcntl and does not run on Windows.
# Use Flask's built-in server for local development on this device.
& $venvPython app.py
