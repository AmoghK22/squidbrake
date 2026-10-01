# Squidbrake installer for Windows (PowerShell).
#   irm <server>/install.ps1 | iex
# With $env:SQUIDBRAKE_PILOT and $env:SQUIDBRAKE_PILOT_SERVER set, it also offers to join that pilot (it asks first).
$ErrorActionPreference = "Stop"
Write-Host "`nInstalling Squidbrake (brakes for AI agents)...`n" -ForegroundColor Cyan

$py = $null
foreach ($c in @("py", "python", "python3")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { $py = $cmd.Source; break }
}
if (-not $py) {
    Write-Host "Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "(tick 'Add python.exe to PATH'), open a new PowerShell window, and run this again."
    return
}
$ok = & $py -c "import sys; print(int(sys.version_info >= (3, 10)))"
if ($ok -ne "1") { Write-Host "Python 3.10 or newer is needed (found an older one at $py)." -ForegroundColor Yellow; return }

Push-Location $env:TEMP          # a folder named 'pipx' in the current directory would shadow the module
try {
    & $py -m pip install --user --upgrade --quiet --disable-pip-version-check pipx
    & $py -m pipx install --force squidbrake | Out-Host
    & $py -m pipx ensurepath | Out-Null
} finally { Pop-Location }

$sb = Join-Path $env:USERPROFILE ".local\bin\squidbrake.exe"
if (-not (Test-Path $sb)) { $sb = "squidbrake" }
Write-Host "`nInstalled: $(& $sb --version)" -ForegroundColor Green

if ($env:SQUIDBRAKE_PILOT -and $env:SQUIDBRAKE_PILOT_SERVER) {
    & $sb pilot join $env:SQUIDBRAKE_PILOT --server $env:SQUIDBRAKE_PILOT_SERVER
}

Write-Host "`nNext:" -ForegroundColor Cyan
Write-Host "  1. Open a NEW PowerShell window (so the 'squidbrake' command is found) and run:  squidbrake"
Write-Host "     It prints your keys (save them) and opens the dashboard. Keep that window open."
Write-Host "  2. In another window, connect Claude Code:  squidbrake connect claude-code"
Write-Host "  3. Restart Claude Code and work as usual. Watch it at http://localhost:8080/dashboard`n"
