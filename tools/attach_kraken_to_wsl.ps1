# Automatically re-launch as Administrator if not already elevated
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  KrakenSDR 5-Tuner WSL2 USB Attacher  " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

$usbipd = "C:\Program Files\usbipd-win\usbipd.exe"
if (-not (Test-Path $usbipd)) {
    $usbipd = "usbipd"
}

# Find all RTL-SDR / Kraken tuners (VID 0bda, PID 2838)
$list = & $usbipd list | Out-String
$lines = $list -split "`r?`n"

$krakenBuses = @()
foreach ($line in $lines) {
    if ($line -match "^\s*([0-9]+-[0-9]+)\s+0bda:2838") {
        $krakenBuses += $matches[1]
    }
}

if ($krakenBuses.Count -eq 0) {
    Write-Host "[!] No KrakenSDR tuners detected on USB. Make sure the DATA cable is plugged in!" -ForegroundColor Yellow
} else {
    Write-Host "[+] Found $($krakenBuses.Count) KrakenSDR Tuner(s): $($krakenBuses -join ', ')" -ForegroundColor Green
    foreach ($bus in $krakenBuses) {
        Write-Host " -> Binding & Attaching Bus $bus to WSL2 (Ubuntu)..." -ForegroundColor White
        & $usbipd bind --force --busid $bus 2>$null
        & $usbipd attach --wsl Ubuntu --busid $bus 2>$null
    }
    Write-Host "`n[✓] All KrakenSDR tuners attached to Ubuntu WSL2!" -ForegroundColor Green
}

Write-Host "`nPress any key to close..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
