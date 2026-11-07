<#
    Scheduled-task entry point for the monthly Bundestag corpus update.

    Registered under Windows Task Scheduler as "Bundestag monthly update".
    Runs src/monthly_update.py against the live corpus and writes a timestamped
    log to outputs/logs/.  Review outputs/update_report_<date>.md afterwards
    before giving anyone the data.

    Run it by hand the same way the scheduler does:
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_monthly_update.ps1
#>

$ErrorActionPreference = 'Continue'

# Resolve the project root from this script's own location, so the task keeps
# working if the folder is moved or synced to another machine.
$proj = Split-Path -Parent $PSScriptRoot

$logDir = Join-Path $proj 'outputs\logs'
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}
$stamp = Get-Date -Format 'yyyy-MM-dd_HHmm'
$log   = Join-Path $logDir "monthly_update_$stamp.log"

Set-Location $proj
$env:PYTHONIOENCODING = 'utf-8'

"=== monthly update started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Set-Content $log -Encoding utf8

# pdftotext must resolve to poppler, not the Xpdf build bundled with Git for
# Windows; a wrong engine silently degrades extraction quality.
$pt = (Get-Command pdftotext -ErrorAction SilentlyContinue)
if ($pt) {
    "pdftotext: $($pt.Source)"                  | Add-Content $log -Encoding utf8
    (& $pt.Source -v 2>&1 | Select-Object -First 2) | Add-Content $log -Encoding utf8
} else {
    "WARNING: pdftotext not on PATH - extraction will fall back to pdfminer." | Add-Content $log -Encoding utf8
}

python src/monthly_update.py *>> $log
$code = $LASTEXITCODE

"=== finished $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') exit=$code ===" | Add-Content $log -Encoding utf8

# Keep the twelve most recent logs.
Get-ChildItem $logDir -Filter 'monthly_update_*.log' |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 12 |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
