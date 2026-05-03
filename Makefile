.PHONY: install data test build clean docker-build docker-builder-image docker-build-volume

DOCKER_IMAGE ?= shellclaw-builder

install:
	pip install -e ".[dev]"

data:
	python scripts/fetch_tldr.py

test:
	pytest tests/ -v

build:
	pyinstaller \
		--name shellclaw \
		--onefile \
		--hidden-import shellclaw.tui \
		--hidden-import shellclaw.providers \
		--hidden-import shellclaw.agent \
		--add-data "src/shellclaw/wiki/data:shellclaw/wiki/data" \
		--add-data "src/shellclaw/tui/screens/main.tcss:shellclaw/tui/screens" \
		--add-data "src/shellclaw/tui/screens/onboarding.tcss:shellclaw/tui/screens" \
		--add-data "src/shellclaw/hooks/shellclaw_shell.sh:shellclaw/hooks" \
		--add-data "src/shellclaw/hooks/shellclaw.fish:shellclaw/hooks" \
		--paths src \
		src/shellclaw/__main__.py

# Build Linux binary inside Docker (Debian Bullseye / older glibc). Output: dist/shellclaw
docker-build:
	mkdir -p dist
	docker build -t $(DOCKER_IMAGE) .
	-docker rm -f shellclaw-dist-extract 2>/dev/null
	docker create --name shellclaw-dist-extract $(DOCKER_IMAGE)
	docker cp shellclaw-dist-extract:/build/dist/shellclaw ./dist/shellclaw
	docker rm shellclaw-dist-extract

# Image for bind-mount workflow: docker run --rm -v "$$(pwd):/build" $(DOCKER_IMAGE)-env
docker-builder-image:
	docker build --target builder-env -t $(DOCKER_IMAGE)-env .

docker-build-volume: docker-builder-image
	docker run --rm -v "$$(pwd):/build" $(DOCKER_IMAGE)-env

clean:
	rm -rf dist/ build/ *.spec __pycache__ .pytest_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; true
