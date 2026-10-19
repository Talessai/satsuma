# Satsuma

**The zero-click, offline file converter for Windows.** Start dragging any file or folder
in File Explorer, tap a key, and a wheel opens under the cursor. Drop on a wedge to
convert, edit or act on the files. Everything runs on your PC — nothing is uploaded.

Free and open source · Windows 10/11 (64-bit) · English, 中文, हिन्दी, Español, العربية

| Key (while dragging) | Wheel | What's in it |
|---|---|---|
| **Shift** | Convert formats | 63 targets across images, audio, video, PDF, documents, slides, spreadsheets, subtitles, archives |
| **Ctrl** | File tools | 34 tools: compress, crop, edit, annotate, add background, redact, metadata, collage, create PDF; video/audio trim, split, merge, rotate, speed, normalize…; PDF split, merge, rotate, protect, watermark, page numbers… |
| **Alt** | File actions | Share (Windows share sheet), Float (shelf), Copy, Move, Copy Path, Terminal, Reveal, Rename, Hash |

Keys are configurable in Settings. Drop in the middle of the wheel (or press Esc) to cancel.
Other ways in: click the tray icon for a drop zone, tray → *Open files…*, or enable
**Send to → Satsuma** / the Explorer context menu in Settings.

## Download

Get **Satsuma-Setup.exe** from the [latest release](../../releases/latest) and run it.
It installs for your user only (no admin rights) and can start Satsuma with Windows.
A portable `Satsuma-win64.zip` is attached too.

Builds are not code-signed yet, so Windows SmartScreen may say "Windows protected your
PC": click **More info → Run anyway**.

## Support

Satsuma is free. If it saves you time, you can support its development on Ko-fi
(link in the app: Settings → About, and on the website).

## Formats

- **Images** JPG PNG WebP HEIC TIFF AVIF BMP ICO GIF, SVG input → each other + PDF
- **Audio** MP3 M4A WAV FLAC OGG Opus AIFF WMA
- **Video** MP4 MOV MKV WebM AVI WMV → each other, GIF, MP3
- **PDF** → DOCX PPTX XLSX JPG PNG EPUB TXT MD HTML
- **Documents** DOCX DOC ODT MD TXT RTF RTFD EPUB HTML (PAGES preview) → DOCX PDF MD TXT EPUB HTML ODT RTF
- **Slides** PPT PPTX ODP → PDF JPG PNG
- **Spreadsheets** XLSX XLS XLSM ODS CSV TSV → XLSX ODS CSV TSV PDF HTML
- **Subtitles** SRT ↔ VTT, → TXT
- **Archives** extract ZIP TAR GZ TGZ BZ2 XZ RAR 7Z; repack; pack any selection as ZIP/TAR/TGZ

Office formats use Microsoft Office through COM when installed, then LibreOffice, then a
built-in pandoc/PyMuPDF fallback. Slides need PowerPoint or LibreOffice.

## Languages

English, 中文（简体）, हिन्दी, Español and العربية (right-to-left). Satsuma follows the
Windows display language by default; change it in Settings → General → Language.
Want another language? See [CONTRIBUTING.md](CONTRIBUTING.md).

## Privacy

No network access is needed or used for converting. No telemetry, no accounts.
Settings and the log are stored in `%APPDATA%\Satsuma\`.

## Run from source

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m satsuma
```

## Build

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

Runs the tests, builds `dist\Satsuma\Satsuma.exe` with PyInstaller, self-tests the built
exe (`Satsuma.exe --selftest report.json`), then makes `dist\Satsuma-Setup.exe` (needs
[Inno Setup](https://jrsoftware.org/isinfo.php)) and `dist\Satsuma-win64.zip`.
Pushing a tag like `v0.1.0` builds and publishes a GitHub Release automatically.

## Layout

- `satsuma/app.py` tray app, single instance, Send To handling
- `satsuma/hooks.py` global low-level mouse/keyboard hook (drag + key tap)
- `satsuma/wheel.py` the radial overlay (OLE drop target)
- `satsuma/registry.py` the `Option` registry every converter/tool/action plugs into
- `satsuma/jobs.py` background job runner; `satsuma/ui/toast.py` progress panel
- `satsuma/converters/`, `satsuma/tools/`, `satsuma/actions/` the options
- `satsuma/i18n.py`, `satsuma/locales/` translations
- `installer/` Inno Setup script · `site/` the website · `tests/` pytest suite

## License

© 2026 Talal Alawi. Satsuma is free software under the
[GNU Affero General Public License v3.0 or later](LICENSE). It bundles open-source
components under their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

---

<div dir="rtl">

## بالعربي

**Satsuma** محوّل ملفات لويندوز يشتغل بدون إنترنت وبدون نقرات. ابدأ بسحب أي ملف في
مستكشف الملفات، اضغط **Shift** للتحويل أو **Ctrl** للأدوات أو **Alt** للإجراءات،
تظهر عجلة تحت المؤشر، أفلت الملف على الخيار اللي تبيه. كل شيء يصير على جهازك وما يُرفع
أي ملف.

مجاني ومفتوح المصدر، من تطوير **Talal Alawi**، ويدعم العربية بالكامل
بما فيها الواجهة من اليمين لليسار. حمّل **Satsuma-Setup.exe** من صفحة الإصدارات، وإذا
أفادك التطبيق تقدر تدعمه على Ko-fi.

</div>
