# Database backup (requirement 7.6). Run from the cadence-hub folder:  .\scripts\backup.ps1
# Writes backups\hub-<date>.dump (a pg_dump custom-format file) and keeps the newest 14.
# Back up storage\inbox (uploaded CVs and request files) with your normal file backup; the CV folder is the source of truth for the rest.
$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
New-Item -ItemType Directory -Force backups | Out-Null
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc -f /tmp/hub.dump "$POSTGRES_DB"'
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }
docker compose cp db:/tmp/hub.dump "backups/hub-$stamp.dump"
docker compose exec -T db rm -f /tmp/hub.dump
Get-ChildItem backups -Filter "hub-*.dump" | Sort-Object LastWriteTime -Descending | Select-Object -Skip 14 | Remove-Item
Write-Host "Backup written: backups\hub-$stamp.dump"
