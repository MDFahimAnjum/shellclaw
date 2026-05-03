#!/bin/sh
set -eu
cd /build
pip install --no-cache-dir -e ".[dev]"
python scripts/fetch_tldr.py
exec pyinstaller \
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
