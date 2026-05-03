"""Entry for ``python -m shellclaw`` and PyInstaller (avoids bare-script relative imports)."""

from shellclaw.main import main

if __name__ == "__main__":
    main()
