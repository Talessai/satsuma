"""Release metadata stays consistent between the app, the website and the installer."""
from __future__ import annotations

import json
import re
from pathlib import Path

from satsuma import about

ROOT = Path(__file__).resolve().parents[1]


def test_site_config_matches_app():
    cfg = json.loads((ROOT / "site" / "config.json").read_text(encoding="utf-8"))
    assert cfg["version"] == about.VERSION
    assert cfg["author"] == about.AUTHOR
    assert cfg["year"] == about.COPYRIGHT_YEAR
    assert cfg["repo_url"] == about.REPO_URL
    assert cfg["donate_url"] == about.DONATE_URL
    assert cfg["site_url"] == about.WEBSITE_URL


def test_installer_matches_app():
    iss = (ROOT / "installer" / "satsuma.iss").read_text(encoding="utf-8-sig")
    assert f'#define AppPublisher "{about.AUTHOR}"' in iss
    assert re.search(r'#define AppVersion "(.+?)"', iss).group(1) == about.VERSION


def test_license_files_present():
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for name in ("FFmpeg", "pandoc", "PyMuPDF", "PySide6"):
        assert name.lower() in notices.lower()
