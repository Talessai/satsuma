"""The download website (site/) builds, is complete in every language and stays original."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[1] / "site"
LANGS = ["en", "ar", "zh", "hi", "es"]


def _load_build():
    spec = importlib.util.spec_from_file_location("satsuma_site_build", SITE / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("site") / "public"
    # Run it the way CI does: a separate interpreter, standard library only.
    subprocess.run([sys.executable, str(SITE / "build.py"), "--out", str(out)], check=True)
    return out


def _pages(out: Path) -> dict[str, str]:
    return {
        code: (out / ("index.html" if code == "en" else f"{code}/index.html")).read_text(encoding="utf-8")
        for code in LANGS
    }


def test_all_pages_exist(built: Path) -> None:
    for code in LANGS:
        assert (built / ("index.html" if code == "en" else f"{code}/index.html")).is_file(), code
    for name in ("404.html", "robots.txt", "assets/css/site.css", "assets/img/favicon.ico"):
        assert (built / name).is_file(), name


def test_lang_and_direction(built: Path) -> None:
    pages = _pages(built)
    assert re.search(r'<html lang="ar" dir="rtl">', pages["ar"])
    for code in ("en", "zh", "hi", "es"):
        assert 'dir="ltr">' in pages[code].split("\n", 2)[1], code
    for code, page in pages.items():
        assert page.count('rel="alternate" hreflang=') == 6, code  # 5 languages + x-default


def test_i18n_keys_match_english() -> None:
    build = _load_build()
    ref = build.flatten(json.loads((SITE / "i18n" / "en.json").read_text(encoding="utf-8")))
    for code in LANGS:
        keys = build.flatten(json.loads((SITE / "i18n" / f"{code}.json").read_text(encoding="utf-8")))
        assert keys == ref, f"{code}: missing {sorted(ref - keys)}, extra {sorted(keys - ref)}"


def test_no_tangerine(built: Path) -> None:
    for f in built.rglob("*"):
        if f.suffix in {".html", ".txt", ".xml", ".css"}:
            assert "tangerine" not in f.read_text(encoding="utf-8").lower(), f


def test_assets_referenced_exist(built: Path) -> None:
    for code, page in _pages(built).items():
        base = built if code == "en" else built / code
        for ref in re.findall(r'(?:src|href|srcset)="([^"#:]+?)(?:\s+\d+w)?"', page):
            for part in ref.split(","):
                path = part.strip().split(" ")[0]
                if path and not path.startswith(("http", "mailto")):
                    target = (base / path).resolve()
                    if path.endswith("/"):
                        target = target / "index.html"
                    assert target.exists(), f"{code}: {path}"


def test_empty_urls_render_gracefully(built: Path) -> None:
    cfg = json.loads((SITE / "config.json").read_text(encoding="utf-8"))
    page = _pages(built)["en"]
    if not cfg["repo_url"]:
        assert 'href="#download"' in page and "github.com" not in page
    else:
        assert f'{cfg["repo_url"].rstrip("/")}/releases/latest/download/Satsuma-Setup.exe' in page
    if not cfg["donate_url"]:
        assert "Support on Ko-fi" not in page
    assert (built / "sitemap.xml").exists() == bool(cfg["site_url"])


def test_repo_links_can_be_hidden(tmp_path: Path) -> None:
    build = _load_build()
    cfg = {"version": "9.9.9", "site_url": "", "repo_url": "https://github.com/x/satsuma", "show_repo_links": False,
           "donate_url": "", "download_size_mb": 123, "author": "A", "year": 2030}
    out = tmp_path / "out"
    build.build(out, cfg)
    for page in [p.read_text(encoding="utf-8") for p in out.rglob("*.html")]:
        github_links = set(re.findall(r'href="(https://github\.com[^"]*)"', page))
        assert github_links <= {"https://github.com/x/satsuma/releases/latest/download/Satsuma-Setup.exe",
                                "https://github.com/x/satsuma/releases/latest/download/Satsuma-win64.zip"}, github_links
        assert "GitHub" not in page and "github" not in page.replace("github.com/x/satsuma/releases/latest/download", "")


def test_links_follow_config(tmp_path: Path) -> None:
    build = _load_build()
    cfg = {"version": "9.9.9", "site_url": "https://example.org/satsuma/", "repo_url": "https://github.com/x/satsuma",
           "donate_url": "https://ko-fi.com/x", "download_size_mb": 123, "author": "A", "year": 2030}
    out = tmp_path / "out"
    build.build(out, cfg)
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "https://github.com/x/satsuma/releases/latest/download/Satsuma-Setup.exe" in page
    assert "https://github.com/x/satsuma/releases/latest/download/Satsuma-win64.zip" in page
    assert 'href="https://ko-fi.com/x"' in page
    assert "9.9.9" in page and "123" in page
    assert '<link rel="canonical" href="https://example.org/satsuma/">' in page
    assert 'content="https://example.org/satsuma/assets/img/og-en.png"' in page
    sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>https://example.org/satsuma/ar/</loc>" in sitemap
    assert "Sitemap: https://example.org/satsuma/sitemap.xml" in (out / "robots.txt").read_text(encoding="utf-8")
