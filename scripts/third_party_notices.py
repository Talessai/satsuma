"""Write THIRD_PARTY_NOTICES.md: every component bundled in the Windows build.

    python scripts/third_party_notices.py

Lists the Python packages installed in the build environment (minus build/test
tools) with their license and project URL, plus the native programs and libraries
that ship inside them (FFmpeg, pandoc, Qt, MuPDF, ...), with where to get their
source code, as the GPL/AGPL/LGPL require.
"""
from __future__ import annotations

import importlib.metadata as md
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_ONLY = {"pyinstaller", "pyinstaller-hooks-contrib", "pytest", "pluggy", "iniconfig", "pip",
              "setuptools", "wheel", "altgraph", "pefile", "pywin32-ctypes", "packaging", "colorama"}

NATIVE = [
    ("FFmpeg {ffmpeg}", "GPL-3.0-or-later (this build includes x264/x265)",
     "https://ffmpeg.org/download.html — source of the bundled build: https://github.com/imageio/imageio-binaries"),
    ("pandoc {pandoc}", "GPL-2.0-or-later", "https://github.com/jgm/pandoc/releases"),
    ("Qt 6 (via PySide6)", "LGPL-3.0", "https://download.qt.io/official_releases/qt/ — the Qt DLLs are separate "
     "files in the install folder and can be replaced"),
    ("MuPDF (via PyMuPDF)", "AGPL-3.0", "https://mupdf.com/releases"),
    ("libheif, libde265, x265, libaom, dav1d (via pillow-heif / Pillow)", "LGPL-3.0 / GPL-2.0 / BSD-2-Clause",
     "https://github.com/strukturag/libheif, https://github.com/videolan/x265, https://aomedia.googlesource.com/aom"),
    ("OpenCV (via opencv-python-headless)", "Apache-2.0", "https://github.com/opencv/opencv"),
]


def _license(dist: md.Distribution) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr
    lic = (meta.get("License") or "").strip()
    if lic and len(lic) < 80 and "\n" not in lic:
        return lic
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::")]
    return ", ".join(classifiers) or (lic.splitlines()[0][:80] if lic else "see project")


def _url(dist: md.Distribution) -> str:
    meta = dist.metadata
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("source", "source code", "repository", "homepage", "home"):
            return url.strip()
    return meta.get("Home-page") or ""


def _versions() -> dict:
    v = {"ffmpeg": "", "pandoc": ""}
    try:
        import imageio_ffmpeg

        m = re.search(r"v(\d+(?:\.\d+)*)", Path(imageio_ffmpeg.get_ffmpeg_exe()).name)
        v["ffmpeg"] = m.group(1) if m else ""
    except Exception:
        pass
    try:
        import pypandoc

        v["pandoc"] = pypandoc.get_pandoc_version()
    except Exception:
        pass
    return v


def main() -> None:
    from satsuma import about

    dists = {}
    for d in md.distributions():
        name = d.metadata["Name"]
        if name and name.lower() not in BUILD_ONLY:
            dists[name.lower()] = d
    lines = [
        f"# Third-party notices for {about.APP_NAME} {about.VERSION}",
        "",
        f"{about.APP_NAME} is {about.COPYRIGHT}, licensed under the {about.LICENSE_NAME} "
        f"({about.LICENSE_ID}); see LICENSE.",
        "",
        "The Windows build bundles the components below. Each remains under its own license.",
        "",
        "## Programs and native libraries",
        "",
        "| Component | License | Source |",
        "|---|---|---|",
    ]
    v = _versions()
    for name, lic, src in NATIVE:
        lines.append(f"| {name.format(**v).strip()} | {lic} | {src} |")
    lines += ["", "## Python packages", "", "| Package | Version | License | Project |", "|---|---|---|---|"]
    for key in sorted(dists):
        d = dists[key]
        lic = _license(d).replace("|", "/")
        lines.append(f"| {d.metadata['Name']} | {d.version} | {lic} | {_url(d)} |")
    lines += ["", "The complete source code of Satsuma, and of this list, is available from the project "
              "repository" + (f": {about.REPO_URL}" if about.REPO_URL else ".") , ""]
    (ROOT / "THIRD_PARTY_NOTICES.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote THIRD_PARTY_NOTICES.md ({len(dists)} packages)")


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(ROOT))
    main()
