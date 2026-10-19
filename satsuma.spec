# PyInstaller spec: builds dist/Satsuma/Satsuma.exe (one-folder, windowed).
#   .venv\Scripts\pyinstaller satsuma.spec --noconfirm
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

import sys

sys.path.insert(0, ".")
from satsuma.registry import PLUGIN_MODULES  # noqa: E402
from satsuma import about  # noqa: E402
from PyInstaller.utils.win32.versioninfo import (  # noqa: E402
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

_v = tuple(int(x) for x in (about.VERSION.split(".") + ["0", "0", "0"])[:4])
# Shown in Explorer: right-click Satsuma.exe > Properties > Details.
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_v, prodvers=_v),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", about.AUTHOR),
            StringStruct("FileDescription", "Satsuma - offline file converter"),
            StringStruct("FileVersion", about.VERSION),
            StringStruct("InternalName", "Satsuma"),
            StringStruct("LegalCopyright", f"{about.COPYRIGHT}. {about.LICENSE_ID}"),
            StringStruct("OriginalFilename", "Satsuma.exe"),
            StringStruct("ProductName", "Satsuma"),
            StringStruct("ProductVersion", about.VERSION),
            StringStruct("Comments", about.REPO_URL or "Free and open-source software"),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

hidden = (
    collect_submodules("satsuma")
    + [f"satsuma.{m}" for m in PLUGIN_MODULES]
    + ["satsuma.media", "satsuma.office", "satsuma.integration", "satsuma.selftest",
       "satsuma.ui.settings_dialog", "satsuma.ui.welcome"]
    + collect_submodules("pillow_heif")
    + collect_submodules("winrt")
    + collect_submodules("win32com")
    + ["pythoncom", "pywintypes", "win32clipboard", "PySide6.QtSvg", "PySide6.QtNetwork"]
)

datas = (
    [("satsuma/locales/*.json", "satsuma/locales")]                # UI translations
    + [("LICENSE", "."), ("THIRD_PARTY_NOTICES.md", ".")]
    + collect_data_files("imageio_ffmpeg", include_py_files=False)   # bundled ffmpeg.exe
    + collect_data_files("pypandoc")                                # bundled pandoc.exe
    + collect_data_files("cv2", includes=["data/*.xml"])             # face cascades
    + collect_data_files("pdf2docx")
    + collect_data_files("docx")
    + collect_data_files("pptx")
)

binaries = collect_dynamic_libs("pillow_heif") + collect_dynamic_libs("pymupdf")

excludes = [
    "tkinter", "matplotlib", "IPython", "pytest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick3D", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtQml",
    "PySide6.QtQuick", "PySide6.QtDesigner", "PySide6.QtBluetooth", "PySide6.QtPositioning",
]

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=excludes,
    noarchive=False,
)

# Drop big pieces nothing uses: software OpenGL, Qt Quick/QML/PDF, extra face cascades.
_KEEP_CASCADES = ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml")


def _unused(dest: str) -> bool:
    d = dest.replace("\\", "/").lower()
    name = d.rsplit("/", 1)[-1]
    if name in ("opengl32sw.dll", "qt6quick.dll", "qt6qml.dll", "qt6qmlmodels.dll", "qt6qmlmeta.dll",
                "qt6qmlworkerscript.dll", "qt6pdf.dll", "qpdf.dll", "qt6virtualkeyboard.dll"):
        return True
    if "/qml/" in d or "/translations/" in d:
        return True
    return "cv2/data/" in d and name.endswith(".xml") and name not in _KEEP_CASCADES


a.binaries = [b for b in a.binaries if not _unused(b[0])]
a.datas = [b for b in a.datas if not _unused(b[0])]

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Satsuma",
    console=False,
    icon="satsuma/resources/satsuma.ico",
    version=version_info,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Satsuma")

# License files next to Satsuma.exe (PyInstaller puts datas under _internal\).
import shutil  # noqa: E402

for _f in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
    shutil.copy(_f, f"{DISTPATH}/Satsuma/{_f}")
