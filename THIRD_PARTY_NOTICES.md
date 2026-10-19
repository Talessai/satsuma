# Third-party notices for Satsuma 0.1.0

Satsuma is © 2026 Talal Alawi, licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later); see LICENSE.

The Windows build bundles the components below. Each remains under its own license.

## Programs and native libraries

| Component | License | Source |
|---|---|---|
| FFmpeg 7.1 | GPL-3.0-or-later (this build includes x264/x265) | https://ffmpeg.org/download.html — source of the bundled build: https://github.com/imageio/imageio-binaries |
| pandoc 3.9 | GPL-2.0-or-later | https://github.com/jgm/pandoc/releases |
| Qt 6 (via PySide6) | LGPL-3.0 | https://download.qt.io/official_releases/qt/ — the Qt DLLs are separate files in the install folder and can be replaced |
| MuPDF (via PyMuPDF) | AGPL-3.0 | https://mupdf.com/releases |
| libheif, libde265, x265, libaom, dav1d (via pillow-heif / Pillow) | LGPL-3.0 / GPL-2.0 / BSD-2-Clause | https://github.com/strukturag/libheif, https://github.com/videolan/x265, https://aomedia.googlesource.com/aom |
| OpenCV (via opencv-python-headless) | Apache-2.0 | https://github.com/opencv/opencv |

## Python packages

| Package | Version | License | Project |
|---|---|---|---|
| defusedxml | 0.7.1 | PSFL | https://github.com/tiran/defusedxml |
| EbookLib | 0.20 | GNU Affero General Public License | https://github.com/aerkalov/ebooklib |
| et_xmlfile | 2.0.0 | MIT | https://foss.heptapod.net/openpyxl/et_xmlfile |
| fire | 0.7.1 | Apache-2.0 | https://github.com/google/python-fire |
| fonttools | 4.67.0 | MIT | http://github.com/fonttools/fonttools |
| imageio-ffmpeg | 0.6.0 | BSD-2-Clause | https://github.com/imageio/imageio-ffmpeg |
| lxml | 6.1.3 | BSD-3-Clause | https://github.com/lxml/lxml |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | https://numpy.org |
| odfpy | 1.4.1 | Apache Software License, GNU General Public License (GPL), GNU Library or Lesser General Public License (LGPL) | https://github.com/eea/odfpy |
| opencv-python-headless | 4.14.0.94 | Apache 2.0 | https://github.com/opencv/opencv-python |
| openpyxl | 3.1.5 | MIT | https://foss.heptapod.net/openpyxl/openpyxl |
| pandas | 3.0.6 | BSD License | https://pandas.pydata.org |
| pdf2docx | 0.5.13 | MIT | https://artifex.com/ |
| pillow | 13.0.0 | MIT-CMU | https://python-pillow.org |
| pillow_heif | 1.8.1 | BSD-3-Clause | https://github.com/bigcat88/pillow_heif |
| Pygments | 2.21.0 | BSD-2-Clause | https://pygments.org |
| pymupdf | 1.28.2 | Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License | https://github.com/pymupdf/pymupdf |
| pypandoc_binary | 1.17 | MIT | https://github.com/JessicaTegner/pypandoc |
| PySide6 | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | https://pyside.org |
| PySide6_Addons | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | https://pyside.org |
| PySide6_Essentials | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | https://pyside.org |
| PySide6_Pdf | 6.12.0.140 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | https://pyside.org |
| PySide6_WebEngine | 6.12.0.140 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | https://pyside.org |
| python-dateutil | 2.9.0.post0 | Dual License | https://github.com/dateutil/dateutil |
| python-docx | 1.2.0 | MIT | https://github.com/python-openxml/python-docx |
| python-pptx | 1.0.2 | MIT | https://github.com/scanny/python-pptx |
| pywin32 | 312 | PSF | https://github.com/mhammond/pywin32 |
| rarfile | 4.5 | ISC | https://github.com/markokr/rarfile |
| shiboken6 | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | https://pyside.org |
| six | 1.17.0 | MIT | https://github.com/benjaminp/six |
| termcolor | 3.3.0 | MIT | https://github.com/termcolor/termcolor |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| tzdata | 2026.5 | Apache-2.0 | https://github.com/python/tzdata |
| winrt-runtime | 3.2.1 | MIT | https://github.com/pywinrt/pywinrt |
| winrt-Windows.Foundation | 3.2.1 | MIT | https://github.com/pywinrt/pywinrt |
| winrt-Windows.Foundation.Collections | 3.2.1 | MIT | https://github.com/pywinrt/pywinrt |
| winrt-Windows.Globalization | 3.2.1 | MIT | https://github.com/pywinrt/pywinrt |
| winrt-Windows.Graphics.Imaging | 3.2.1 | MIT | https://github.com/pywinrt/pywinrt |
| winrt-Windows.Media.Ocr | 3.2.1 | MIT | https://github.com/pywinrt/pywinrt |
| winrt-Windows.Storage | 3.2.1 | MIT | https://github.com/pywinrt/pywinrt |
| winrt-Windows.Storage.Streams | 3.2.1 | MIT | https://github.com/pywinrt/pywinrt |
| xlrd | 2.0.2 | BSD | http://www.python-excel.org/ |
| xlsxwriter | 3.2.9 | BSD-2-Clause | https://github.com/jmcnamara/XlsxWriter |

The complete source code of Satsuma, and of this list, is available from the project repository: https://github.com/Talessai/satsuma
