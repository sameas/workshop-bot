COMPOSE ?= docker compose
PY_IMAGE := python:3.12-slim-bookworm
BACKUP_DIR ?= backups

.PHONY: test lint build up down logs pull backup lock

test:
	python -m pytest -q

lint:
	ruff check bot tests

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100 bot

pull:
	$(COMPOSE) pull
	$(COMPOSE) up -d

# sqlite online backup, безопасно при работающем боте
backup:
	$(COMPOSE) exec -T bot python -c "import sqlite3; sqlite3.connect('/data/bot.db').backup(sqlite3.connect('/data/backup.db'))"
	mkdir -p $(BACKUP_DIR)
	cp data/backup.db $(BACKUP_DIR)/bot-$$(date +%F).db
	$(COMPOSE) exec -T bot rm -f /data/backup.db

lock:
	head -n 1 requirements.lock > requirements.lock.tmp
	docker run --rm -v "$(CURDIR)/requirements.txt":/w/requirements.txt:ro $(PY_IMAGE) \
		sh -c 'pip install -q --root-user-action=ignore -r /w/requirements.txt && pip freeze' >> requirements.lock.tmp
	mv requirements.lock.tmp requirements.lock
