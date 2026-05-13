.PHONY: help install dev-setup start-deps stop-deps scheduler worker test clean

help:
	@echo "Email Outreach Service - Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install      - Install Python dependencies"
	@echo "  make dev-setup    - Set up development environment"
	@echo ""
	@echo "Development:"
	@echo "  make start-deps   - Start Redis (Docker)"
	@echo "  make stop-deps    - Stop Redis"
	@echo "  make scheduler    - Run scheduler locally"
	@echo "  make worker       - Run Celery worker locally"
	@echo ""
	@echo "Utilities:"
	@echo "  make test         - Run tests"
	@echo "  make clean        - Clean up temporary files"

install:
	pip install -r requirements.txt

dev-setup:
	@echo "Setting up development environment..."
	@if [ ! -f .env ]; then cp .env.example .env; echo "Created .env file - please update with your values"; fi
	pip install -r requirements.txt
	@echo "Development environment ready!"

start-deps:
	@echo "Starting Redis..."
	docker-compose up -d redis
	@echo "Waiting for services to be ready..."
	@sleep 5
	@echo "Services are ready!"

stop-deps:
	@echo "Stopping dependencies..."
	docker-compose down

scheduler:
	@echo "Starting scheduler..."
	python run_scheduler.py

scheduler-once:
	@echo "Running scheduler once..."
	python run_scheduler.py --once

worker:
	@echo "Starting Celery worker..."
	celery -A app.workers.celery_app worker --loglevel=info --concurrency=4

worker-dev:
	@echo "Starting Celery worker (development mode)..."
	celery -A app.workers.celery_app worker --loglevel=debug --concurrency=1

flower:
	@echo "Starting Flower (Celery monitoring)..."
	celery -A app.workers.celery_app flower

test:
	@echo "Running tests..."
	pytest

clean:
	@echo "Cleaning up..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.coverage" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleanup complete!"

# Database migrations (requires Alembic setup)
migrate-create:
	alembic revision --autogenerate -m "$(message)"

migrate-up:
	alembic upgrade head

migrate-down:
	alembic downgrade -1

# Full stack
run-all: start-deps
	@echo "Starting all services..."
	@trap 'make stop-all' INT; \
	(make worker &); \
	(make scheduler)

stop-all:
	@echo "Stopping all services..."
	pkill -f "celery -A app.workers.celery_app worker" || true
	pkill -f "python run_scheduler.py" || true
	make stop-deps
