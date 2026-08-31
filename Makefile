.PHONY: install test lint up down logs smoke reset

install:
	python -m pip install -r requirements-dev.txt

test:
	PYTHONPATH=.:src pytest -q

lint:
	ruff check src tests scripts

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

smoke:
	python scripts/smoke_test.py

reset:
	curl -X POST http://localhost:8004/v1/reset
