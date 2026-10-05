#!/bin/sh
# Same as backup.ps1 for WSL / Linux / make.
set -e
stamp=$(date +%Y%m%d-%H%M)
mkdir -p backups
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc -f /tmp/hub.dump "$POSTGRES_DB"'
docker compose cp db:/tmp/hub.dump "backups/hub-$stamp.dump"
docker compose exec -T db rm -f /tmp/hub.dump
ls -1t backups/hub-*.dump | tail -n +15 | xargs -r rm
echo "Backup written: backups/hub-$stamp.dump"
