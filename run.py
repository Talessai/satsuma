"""Launcher used by PyInstaller and for `python run.py`."""
from satsuma.app import main

if __name__ == "__main__":
    raise SystemExit(main())
