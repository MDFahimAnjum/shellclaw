# syntax=docker/dockerfile:1
# Reproducible Linux amd64 binary with older glibc (Debian Bullseye) for broad distro compatibility.
# Python 3.11 matches pyproject requires-python (>=3.11).

ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim-bullseye AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
	binutils \
	build-essential \
	ca-certificates \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Mount host repo at /build and run: same steps as CI + Makefile build.
FROM base AS builder-env
COPY docker/entrypoint-volume-build.sh /usr/local/bin/entrypoint-volume-build.sh
RUN chmod +x /usr/local/bin/entrypoint-volume-build.sh
ENTRYPOINT ["/usr/local/bin/entrypoint-volume-build.sh"]

# Default: build inside the image (no bind-mount).
FROM base AS artifact
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir -e ".[dev]" \
	&& python scripts/fetch_tldr.py \
	&& pyinstaller \
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
