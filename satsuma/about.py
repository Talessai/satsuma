"""Who made Satsuma, its license, and where to find it. The one place to edit these.

Empty URLs are hidden in the UI until they are filled in.
"""
from __future__ import annotations

from . import __version__

APP_NAME = "Satsuma"
VERSION = __version__
AUTHOR = "Talal Alawi"
COPYRIGHT_YEAR = 2026
COPYRIGHT = f"© {COPYRIGHT_YEAR} {AUTHOR}"
LICENSE_ID = "AGPL-3.0-or-later"
LICENSE_NAME = "GNU Affero General Public License v3.0 or later"

REPO_URL = "https://github.com/Talessai/satsuma"
DONATE_URL = "https://ko-fi.com/talalalawi"
WEBSITE_URL = "https://talessai.github.io/satsuma/"


def issues_url() -> str:
    return f"{REPO_URL}/issues" if REPO_URL else ""
