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
if (-not $py -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "Python isn't installed: installing Python 3.12 with winget (a minute or two)..." -ForegroundColor Cyan
    winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $cand = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $cand) { $py = $cand }
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

if ($env:SQUIDBRAKE_URL -and $env:SQUIDBRAKE_AGENT_KEY) {
    # a hosted dashboard: nothing to run locally, just route Claude Code through it
    if ($env:SQUIDBRAKE_AGENT_KEY -like "*YOUR_AGENT_KEY*") {
        Write-Host "`nPut your agent key (from your start page) in place of gw_YOUR_AGENT_KEY and run it again." -ForegroundColor Yellow
        return
    }
    try {
        $me = Invoke-RestMethod "$($env:SQUIDBRAKE_URL)/v1/me" -Headers @{ "X-Gateway-Key" = $env:SQUIDBRAKE_AGENT_KEY } -TimeoutSec 20
    } catch {
        Write-Host "`nCouldn't reach your dashboard with that key ($($_.Exception.Message)). Check the key and run it again." -ForegroundColor Yellow
        return
    }
    Write-Host "`n  [OK] Your dashboard answers (signed in as '$($me.client)')." -ForegroundColor Green
    if ((Get-Command claude -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $env:USERPROFILE ".claude"))) {
        & $sb connect claude-code --url $env:SQUIDBRAKE_URL --key $env:SQUIDBRAKE_AGENT_KEY --yes --hook-only | Out-Null
        Write-Host "  [OK] Claude Code: every tool call (commands, edits, web, MCP) goes through it." -ForegroundColor Green
    }
    # every other coding agent installed here: its terminal commands and file actions (hooks) ...
    & $sb connect agents --agent all --url $env:SQUIDBRAKE_URL --key $env:SQUIDBRAKE_AGENT_KEY --yes | ForEach-Object { Write-Host "  $_" }
    # ... and its own MCP servers (GitHub, Stripe, databases...) go through it too
    & $sb connect guard --agent all --url $env:SQUIDBRAKE_URL --key $env:SQUIDBRAKE_AGENT_KEY --yes | ForEach-Object { Write-Host "  $_" }
    Write-Host "`nLast step: close and reopen your agents (Claude Code, Cursor, ...), then work as usual."
    Write-Host "Your dashboard: $($env:SQUIDBRAKE_URL)/dashboard`n"
    return
}

if ($env:SQUIDBRAKE_PILOT -and $env:SQUIDBRAKE_PILOT_SERVER) {
    & $sb pilot join $env:SQUIDBRAKE_PILOT --server $env:SQUIDBRAKE_PILOT_SERVER
}

Write-Host "`nNext:" -ForegroundColor Cyan
Write-Host "  1. Open a NEW PowerShell window (so the 'squidbrake' command is found) and run:  squidbrake"
Write-Host "     It prints your keys (save them) and opens the dashboard. Keep that window open."
Write-Host "  2. In another window, connect Claude Code:  squidbrake connect claude-code"
Write-Host "  3. Restart Claude Code and work as usual. Watch it at http://localhost:8080/dashboard`n"
