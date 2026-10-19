"""Shared fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _english_ui():
    """Every test starts and ends in English, left-to-right.

    Autouse fixtures are torn down after a test's own monkeypatching is undone, so a
    test that patched the catalog can't leave a translated catalog behind.
    """
    from satsuma import i18n

    i18n.set_language("en")
    yield
    i18n.set_language("en")
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.setLayoutDirection(Qt.LeftToRight)
    except ImportError:
        pass
