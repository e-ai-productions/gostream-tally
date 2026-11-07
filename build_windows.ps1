# Builds one Windows program (gostream-tally.exe) that runs without Python.
# Needs Python 3.10 or newer. After building, start dist\gostream-tally.exe.

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "  GoStream Tally - build the program" -ForegroundColor Cyan
Write-Host "  ---------------------------------------------"
Write-Host ""

# 1. Is Python er?
$python = $null
foreach ($kandidaat in @("python", "py")) {
    if (Get-Command $kandidaat -ErrorAction SilentlyContinue) { $python = $kandidaat; break }
}
if (-not $python) {
    Write-Host "  Python was not found." -ForegroundColor Red
    Write-Host "  Install Python 3 from https://www.python.org/downloads/windows/"
    Write-Host "  and tick 'Add python.exe to PATH' during setup."
    Read-Host "  Press Enter to close"
    exit 1
}
Write-Host "  Python: $(& $python --version 2>&1)"

# 2. PyInstaller
Write-Host ""
Write-Host "  Fetching PyInstaller..." -ForegroundColor Cyan
& $python -m pip install --quiet --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) {
    Write-Host "  That did not work. Check your internet connection." -ForegroundColor Red
    Read-Host "  Press Enter to close"
    exit 1
}

# 3. Bouwen
Write-Host "  Building (takes about a minute)..." -ForegroundColor Cyan
& $python -m PyInstaller --onefile --name gostream-tally --clean --noconfirm gostream_tally.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "  The build failed. The message above says why." -ForegroundColor Red
    Read-Host "  Press Enter to close"
    exit 1
}

# 4. Klaar
$exe = Join-Path (Get-Location).Path "dist\gostream-tally.exe"
Write-Host ""
Write-Host "  Done: $exe" -ForegroundColor Green
Write-Host ""
Write-Host "  That single file is all you need. Python is no longer required."
Write-Host "  Put it in its own folder and start it with a double-click."
Write-Host ""
Write-Host "  Note: the firewall asks again, because this is a different program"
Write-Host "  from python.exe."
Write-Host "  Some scanners report 'unknown file'; that belongs to this way of"
Write-Host "  building and is not a bug in the program."
Write-Host ""
if (Test-Path "dist") {
    $map = (Resolve-Path "dist").Path
    Start-Process explorer.exe -ArgumentList $map
}
Read-Host "  Press Enter to close"
