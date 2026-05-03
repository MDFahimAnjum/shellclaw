#!/usr/bin/env python3
"""
Download the tldr-pages cache and convert it to the JSON format
expected by shellclaw/wiki/tldr.py.

Output: src/shellclaw/wiki/data/tldr.json

Schema:
{
  "command_name": {
    "description": "...",
    "examples": [
      {"description": "...", "command": "..."},
      ...
    ]
  }
}
"""

import json
import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

TLDR_ZIP_URL = "https://tldr.sh/assets/tldr.zip"
# Same content layout under pages/{linux,common}/; Docker/datacenter IPs sometimes get 403 from tldr.sh.
TLDR_ZIP_FALLBACK_URL = "https://github.com/tldr-pages/tldr/archive/refs/heads/main.zip"
# tldr.sh (and similar CDNs) often return 403 for urllib's default User-Agent.
_FETCH_HEADERS = {
    "User-Agent": "shellclaw-fetch-tldr/1.0 (+https://github.com/MDFahimAnjum/shellclaw)",
    "Accept": "*/*",
}
OUTPUT_PATH = Path(__file__).parent.parent / "src/shellclaw/wiki/data/tldr.json"


def _download_zip(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers=_FETCH_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _is_zip_payload(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == b"PK\x03\x04"


def parse_tldr_page(content: str) -> dict:
    lines = content.strip().splitlines()
    description_lines: list[str] = []
    examples: list[dict] = []

    current_desc: str | None = None

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(">"):
            description_lines.append(line.lstrip("> "))
        elif line.startswith("-"):
            current_desc = line.lstrip("- ").rstrip(":")
        elif line.startswith("`") and current_desc is not None:
            cmd = line.strip("`")
            examples.append({"description": current_desc, "command": cmd})
            current_desc = None

    return {
        "description": " ".join(description_lines),
        "examples": examples,
    }


def main() -> None:
    print(f"Downloading tldr-pages from {TLDR_ZIP_URL} ...")
    try:
        data = _download_zip(TLDR_ZIP_URL, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print(f"Got HTTP {e.code} from primary URL; trying {TLDR_ZIP_FALLBACK_URL} ...")
            data = _download_zip(TLDR_ZIP_FALLBACK_URL)
        else:
            raise

    if not _is_zip_payload(data):
        print(f"Primary URL did not return a zip archive; trying {TLDR_ZIP_FALLBACK_URL} ...")
        data = _download_zip(TLDR_ZIP_FALLBACK_URL)
    if not _is_zip_payload(data):
        raise RuntimeError(
            "Could not download a valid tldr-pages zip from primary or fallback URLs."
        )

    print("Parsing pages ...")
    result: dict[str, dict] = {}

    with zipfile.ZipFile(BytesIO(data)) as zf:
        for name in zf.namelist():
            # Only Linux and common pages
            if not name.endswith(".md"):
                continue
            if "/linux/" not in name and "/common/" not in name:
                continue

            command_name = Path(name).stem
            raw = zf.read(name).decode("utf-8", errors="replace")
            parsed = parse_tldr_page(raw)
            if parsed["examples"]:
                result[command_name] = parsed

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"Wrote {len(result)} commands to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
