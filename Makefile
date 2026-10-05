# Optional shortcuts. Needs `make` (WSL or installed separately).
# On plain PowerShell, use the full commands in README.md instead.

.PHONY: up down logs test ps reset

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f api worker

test:
	docker compose run --rm api pytest -q tests

ps:
	docker compose ps

reset:
	docker compose down -v