# Restore a backup made by backup.ps1. THIS REPLACES THE CURRENT DATABASE.
# Usage:  .\scripts\restore.ps1 backups\hub-20261005-1200.dump -Yes
param([Parameter(Mandatory = $true)][string]$File, [switch]$Yes)
$ErrorActionPreference = "Stop"
if (-not $Yes) { throw "This replaces the current database. Run again with -Yes to confirm." }
docker compose cp $File db:/tmp/restore.dump
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists /tmp/restore.dump'
docker compose exec -T db rm -f /tmp/restore.dump
Write-Host "Restored from $File"
