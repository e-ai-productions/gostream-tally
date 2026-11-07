# Starts the GoStream Tally bridge on Windows 11.
# Way 1: right-click this file -> "Run with PowerShell"
# Way 2: open PowerShell in this folder and type:
#   powershell -ExecutionPolicy Bypass -File .\start_windows.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host ""
Write-Host "  GoStream Tally" -ForegroundColor Cyan
Write-Host "  ---------------------------------------------" -ForegroundColor DarkGray

function Test-Python($cmd) {
    try { & $cmd.Source --version *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

$python = $null
foreach ($name in @("python", "py", "python3")) {
    $cand = Get-Command $name -ErrorAction SilentlyContinue
    if ($cand -and (Test-Python $cand)) { $python = $cand; break }
}
if (-not $python) {
    Write-Host "  Python 3 was not found on this PC." -ForegroundColor Yellow
    Write-Host "  Install it from https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "  and tick 'Add python.exe to PATH' during setup." -ForegroundColor Yellow
    Read-Host "  Press Enter to close"
    exit 1
}

# Show this PC's own address; you need it on the phone
$ips = Get-NetIPAddress -AddressFamily IPv4 |
       Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
       Select-Object -ExpandProperty IPAddress
if ($ips) {
    Write-Host "  This PC's address:" -ForegroundColor DarkGray
    foreach ($ip in $ips) { Write-Host "    http://${ip}:8080" -ForegroundColor Green }
}
Write-Host ""
Write-Host "  Open one of those addresses on your phone." -ForegroundColor DarkGray
Write-Host "  The control page opens in your browser by itself." -ForegroundColor DarkGray
Write-Host "  Stop: Ctrl+C" -ForegroundColor DarkGray
Write-Host ""

& $python.Source gostream_tally.py
Read-Host "  Press Enter to close"
