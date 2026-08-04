# Docker image and container names
DOCKER_IMAGE = memory-backend
DOCKER_CONTAINER = memory-backend
DOCKER_COMPOSE = docker compose

.PHONY: build up down restart logs shell test clean

## Build the Docker image
build:
	docker build -t $(DOCKER_IMAGE) .

## Start the service (detached)
up:
	$(DOCKER_COMPOSE) up -d

## Stop and remove the container
down:
	$(DOCKER_COMPOSE) down

## Restart the service
restart:
	$(DOCKER_COMPOSE) restart

## Tail logs
logs:
	$(DOCKER_COMPOSE) logs -f

## Open a shell inside the running container
shell:
	docker exec -it $(DOCKER_CONTAINER) /bin/bash

## Run the test suite inside the container
test:
	docker exec -it $(DOCKER_CONTAINER) python -m pytest tests/ -q

## Remove the Docker image and volume (destructive)
clean:
	$(DOCKER_COMPOSE) down -v
	docker rmi $(DOCKER_IMAGE) 2>/dev/null || true

## Show this help
help:
	@grep -E '^##' Makefile | sed 's/## //'
