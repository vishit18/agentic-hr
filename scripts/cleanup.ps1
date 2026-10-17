# Post-meeting cleanup (README section 7, rule 5): delete every local copy of the challenge data.
# Run from the cadence-hub folder:  .\scripts\cleanup.ps1            (shows what it would delete, deletes nothing)
#                                   .\scripts\cleanup.ps1 -Confirm   (asks you to type DELETE, then deletes)
#                                   .\scripts\cleanup.ps1 -Confirm -IncludeCvFolder   (also deletes the CV folder itself)
# Deletes: the database and its volume, built images, uploaded CVs and request files, backups, eval labels and reports,
# test and cache folders. Never touches source code, docs or .env. Does not delete anything outside this project,
# except the CV folder when you pass -IncludeCvFolder.
param(
    [switch]$Confirm,
    [switch]$IncludeCvFolder
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Only paths inside this project, plus the CV folder if asked. Resolved up front so a bad path stops the run.
$targets = @(
    "storage",                       # inbox: uploaded CVs, request attachments, logs
    "backups",                       # pg_dump files
    "eval\labels.jsonl",             # hand labels (names, emails)
    "eval\labels.skeleton.jsonl",
    "eval\report.md",
    "eval\auto_report.md",
    "backend\.pytest_cache",
    ".pytest_cache"
) | ForEach-Object { Join-Path $root $_ } | Where-Object { Test-Path $_ }

$cvFolder = $null
if ($IncludeCvFolder) {
    $envFile = Join-Path $root ".env"
    $line = if (Test-Path $envFile) { Select-String -Path $envFile -Pattern '^CVS_DIR=(.+)$' | Select-Object -First 1 }
    if (-not $line) { throw "CVS_DIR not found in the env file, so the CV folder cannot be located." }
    $cvFolder = (Resolve-Path (Join-Path $root $line.Matches[0].Groups[1].Value.Trim())).Path
    if ((Split-Path -Leaf $cvFolder) -ne "cvs") { throw "Refusing: '$cvFolder' is not a folder named cvs." }
}

Write-Host "This will delete:" -ForegroundColor Yellow
Write-Host "  - Docker containers, the database volume (pgdata) and images built for this project"
$targets | ForEach-Object { Write-Host "  - $_" }
if ($cvFolder) { Write-Host "  - $cvFolder  (the CV folder)" }
Write-Host ""
Write-Host "Not covered here (do these by hand): other copies of the repo or the cvs folder, cloud resources,"
Write-Host "AI chat or tool history that contains CV content, any email or Drive copies."

if (-not $Confirm) { Write-Host "`nDry run only. Re-run with -Confirm to delete." -ForegroundColor Cyan; exit 0 }

$answer = Read-Host "`nType DELETE to continue"
if ($answer -ne "DELETE") { Write-Host "Cancelled. Nothing was deleted."; exit 1 }

# 1. Containers, volumes (the database and its indexes), and the images built for this project
docker compose down -v --rmi local --remove-orphans
if ($LASTEXITCODE -ne 0) { Write-Warning "docker compose down reported a problem. Check Docker Desktop before continuing." }

# 2. Files
foreach ($t in $targets) {
    Remove-Item -LiteralPath $t -Recurse -Force
    Write-Host "Deleted $t"
}
if ($cvFolder) {
    Remove-Item -LiteralPath $cvFolder -Recurse -Force
    Write-Host "Deleted $cvFolder"
}

# 3. Verify
$left = @()
if (docker volume ls -q | Select-String -Pattern "pgdata") { $left += "a pgdata Docker volume" }
$targets | Where-Object { Test-Path $_ } | ForEach-Object { $left += $_ }
if ($cvFolder -and (Test-Path $cvFolder)) { $left += $cvFolder }
if ($left.Count -gt 0) { Write-Warning ("Still present: " + ($left -join "; ")); exit 2 }

Write-Host "`nDone. Everything this script covers is deleted." -ForegroundColor Green
Write-Host "Now send the confirmation email (draft in docs\CLEANUP_EMAIL.md)."
