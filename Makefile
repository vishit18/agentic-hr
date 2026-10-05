# Optional shortcuts. Needs `make` installed (see README).
# Usage: make start | stop | status | logs | test | down | reset
# Note: lines under each name must start with a TAB, not spaces.

.PHONY: up start stop status down logs test ps reset

# Build and run in the foreground (see logs live). Ctrl+C stops it.
up:
	docker compose up --build

# Run in the background, data kept
start:
	docker compose up -d

# Pause everything, data kept
stop:
	docker compose stop

# Show what is running
status:
	docker compose ps

ps:
	docker compose ps

# Remove containers, data kept
down:
	docker compose down

logs:
	docker compose logs -f api worker

test:
	docker compose run --rm api pytest -q tests

# DANGER: also deletes the database volume (all candidates, requests, users)
reset:
	docker compose down -v
.PHONY: backup
backup:
	sh scripts/backup.sh
