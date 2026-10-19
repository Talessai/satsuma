"""Office automation helpers: Microsoft Office via COM, LibreOffice headless as fallback.

Every COM helper runs in the calling (worker) thread: it initialises COM for that
thread, starts a private, invisible Office instance with alerts and macros off,
and always quits it again. Nothing here touches the GUI thread.
"""
from __future__ import annotations

import contextlib
import functools
import gc
import logging
import shutil
import sys
import threading
import uuid
from pathlib import Path
from typing import Callable, TypeVar

from . import proc
from .i18n import tr
from .paths import soffice_exe
from .registry import UserError

log = logging.getLogger(__name__)

# Word WdSaveFormat values
WORD_FORMATS = {"docx": 16, "pdf": 17, "rtf": 6, "odt": 23, "html": 10, "txt": 7, "doc": 0}
# Excel XlFileFormat values
EXCEL_FORMATS = {"xlsx": 51, "xlsm": 52, "xls": 56, "ods": 60, "csv": 62}
PP_SAVE_AS_PDF = 32
MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3

T = TypeVar("T")
_NO_PASSWORD = "satsuma-no-password"
_ppt_lock = threading.Lock()  # PowerPoint is a single-instance server; serialise use


# --------------------------------------------------------------------------- detection

@functools.lru_cache(maxsize=None)
def _progid_registered(progid: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\CLSID"):
            pass
    except OSError:
        return False
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
    except ImportError:
        return False
    return True


def word_available() -> bool:
    return _progid_registered("Word.Application")


def powerpoint_available() -> bool:
    return _progid_registered("PowerPoint.Application")


def excel_available() -> bool:
    return _progid_registered("Excel.Application")


def libreoffice_available() -> bool:
    return soffice_exe() is not None


def _win(path: Path) -> str:
    """Absolute Windows path string, as Office expects."""
    return str(Path(path).resolve())


# --------------------------------------------------------------------------- COM core

class OfficeError(Exception):
    """An Office automation step failed (callers may fall back to something else)."""


def run_com(progid: str, fn: Callable[[object], T], *, hide: bool = True) -> T:
    """Start a private Office instance in this thread, call fn(app), always quit it.

    All COM proxies created inside `fn` are released before COM is uninitialised;
    errors are re-raised as fresh exceptions so no traceback keeps proxies alive.
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    error: Exception | None = None
    result = None
    try:
        try:
            app = win32com.client.DispatchEx(progid)
        except Exception as e:  # pywintypes.com_error
            error = OfficeError(tr("Could not start {app}: {error}").format(app=progid.split(".")[0],
                                                                              error=_msg(e)))
            app = None
        if app is not None:
            if hide:
                with contextlib.suppress(Exception):
                    app.Visible = False
            with contextlib.suppress(Exception):
                app.DisplayAlerts = 1 if progid == "PowerPoint.Application" else False  # ppAlertsNone
            with contextlib.suppress(Exception):
                app.AutomationSecurity = MSO_AUTOMATION_SECURITY_FORCE_DISABLE
            try:
                result = fn(app)
            except UserError as e:
                error = UserError(str(e))
            except Exception as e:
                error = OfficeError(str(e) if isinstance(e, OfficeError) else _msg(e))
            finally:
                with contextlib.suppress(Exception):
                    if progid == "PowerPoint.Application":
                        # Shared instance: never close the user's own presentations.
                        if app.Presentations.Count == 0:
                            app.Quit()
                    else:
                        app.Quit()
            del app
    finally:
        gc.collect()
        pythoncom.CoUninitialize()
    if error is not None:
        raise error
    return result  # type: ignore[return-value]


def word_convert(src: Path, dst: Path, fmt: str) -> Path:
    """Open `src` in Word and save it as `fmt` (docx, pdf, rtf, odt, html, txt, doc)."""
    if not word_available():
        raise OfficeError(tr("Microsoft Word is not available"))
    code = WORD_FORMATS[fmt]
    dst = Path(dst)

    def work(word) -> None:
        try:
            # A bogus password makes protected files fail instead of showing a hidden prompt.
            doc = word.Documents.Open(FileName=_win(src), ConfirmConversions=False, ReadOnly=True,
                                      AddToRecentFiles=False, PasswordDocument=_NO_PASSWORD,
                                      PasswordTemplate=_NO_PASSWORD, Visible=False,
                                      NoEncodingDialog=True)
        except Exception as e:
            raise OfficeError(tr("Word could not open {name}: {error}").format(name=Path(src).name, error=_msg(e)))
        try:
            if fmt == "pdf":
                doc.ExportAsFixedFormat(_win(dst), 17)
            else:
                doc.SaveAs2(_win(dst), code)
        except Exception as e:
            raise OfficeError(tr("Word could not save {name}: {error}").format(name=dst.name, error=_msg(e)))
        finally:
            with contextlib.suppress(Exception):
                doc.Close(0)

    run_com("Word.Application", work)
    if not dst.exists():
        raise OfficeError(tr("Word produced no output"))
    return dst


def excel_convert(src: Path, dst: Path, fmt: str) -> Path:
    """Open `src` in Excel and save as `fmt` (pdf, xlsx, ods, xls, ...)."""
    if not excel_available():
        raise OfficeError(tr("Microsoft Excel is not available"))
    dst = Path(dst)

    def work(xl) -> None:
        with contextlib.suppress(Exception):
            xl.ScreenUpdating = False
        try:
            wb = xl.Workbooks.Open(Filename=_win(src), UpdateLinks=0, ReadOnly=True,
                                   Password=_NO_PASSWORD, IgnoreReadOnlyRecommended=True,
                                   Notify=False, AddToMru=False)
        except Exception as e:
            raise OfficeError(tr("Excel could not open {name}: {error}").format(name=Path(src).name, error=_msg(e)))
        try:
            if fmt == "pdf":
                wb.ExportAsFixedFormat(0, _win(dst))  # xlTypePDF
            else:
                wb.SaveAs(_win(dst), EXCEL_FORMATS[fmt])
        except Exception as e:
            raise OfficeError(tr("Excel could not save {name}: {error}").format(name=dst.name, error=_msg(e)))
        finally:
            with contextlib.suppress(Exception):
                wb.Close(False)

    run_com("Excel.Application", work)
    if not dst.exists():
        raise OfficeError(tr("Excel produced no output"))
    return dst


def powerpoint_to_pdf(src: Path, dst: Path) -> Path:
    if not powerpoint_available():
        raise OfficeError(tr("Microsoft PowerPoint is not available"))
    dst = Path(dst)

    def work(pp) -> None:
        pres = _ppt_open(pp, src)
        try:
            pres.SaveAs(_win(dst), PP_SAVE_AS_PDF)
        except Exception as e:
            raise OfficeError(tr("PowerPoint could not save {name}: {error}").format(name=dst.name, error=_msg(e)))
        finally:
            with contextlib.suppress(Exception):
                pres.Close()

    with _ppt_lock:
        run_com("PowerPoint.Application", work, hide=False)
    if not dst.exists():
        raise OfficeError(tr("PowerPoint produced no output"))
    return dst


def powerpoint_export_images(src: Path, out_dir: Path, stem: str, ext: str,
                             width: int = 1920, ctx=None) -> list[Path]:
    """Export every slide as an image: out_dir/'<stem> 01.<ext>' ..."""
    if not powerpoint_available():
        raise OfficeError(tr("Microsoft PowerPoint is not available"))
    filt = "JPG" if ext in ("jpg", "jpeg") else "PNG"
    outs: list[Path] = []

    def work(pp) -> None:
        pres = _ppt_open(pp, src)
        try:
            sw, sh = float(pres.PageSetup.SlideWidth), float(pres.PageSetup.SlideHeight)
            height = max(1, round(width * sh / sw))
            n = int(pres.Slides.Count)
            pad = max(2, len(str(n)))
            for i in range(1, n + 1):
                if ctx is not None:
                    ctx.check_cancel()
                    ctx.progress((i - 1) / max(n, 1), tr("Slide {n}/{total}").format(n=i, total=n))
                path = out_dir / f"{stem} {i:0{pad}d}.{ext}"
                pres.Slides(i).Export(_win(path), filt, width, height)
                outs.append(path)
        except UserError:
            raise
        except Exception as e:
            raise OfficeError(tr("PowerPoint could not export slides: {error}").format(error=_msg(e)))
        finally:
            with contextlib.suppress(Exception):
                pres.Close()

    with _ppt_lock:
        run_com("PowerPoint.Application", work, hide=False)
    return outs


def _ppt_open(pp, src: Path):
    try:
        # ReadOnly, Untitled, WithWindow=False
        return pp.Presentations.Open(_win(src), True, False, False)
    except Exception as e:
        raise OfficeError(tr("PowerPoint could not open {name}: {error}").format(name=Path(src).name,
                                                                          error=_msg(e)))


def _msg(e: Exception) -> str:
    """Readable text of a pywintypes.com_error (Office's own description if present)."""
    args = getattr(e, "args", ())
    if len(args) >= 3 and isinstance(args[2], tuple) and len(args[2]) >= 3 and args[2][2]:
        return str(args[2][2]).strip()
    if len(args) >= 2 and isinstance(args[1], str):
        return args[1]
    return str(e)


# --------------------------------------------------------------------------- LibreOffice

def soffice_convert(src: Path, out_dir: Path, fmt: str, ctx=None, filter_name: str | None = None) -> Path:
    """`soffice --headless --convert-to fmt` into out_dir; returns the produced file."""
    exe = soffice_exe()
    if not exe:
        raise OfficeError(tr("LibreOffice is not installed"))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = out_dir / f".lo-profile-{uuid.uuid4().hex[:8]}"
    target = f"{fmt}:{filter_name}" if filter_name else fmt
    try:
        proc.run([exe, f"-env:UserInstallation={profile.resolve().as_uri()}", "--headless",
                  "--norestore", "--nolockcheck", "--convert-to", target, "--outdir",
                  _win(out_dir), _win(src)], ctx)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    produced = out_dir / f"{Path(src).stem}.{fmt}"
    if not produced.exists():
        raise OfficeError(tr("LibreOffice produced no output"))
    return produced


def convert_with_office(kind: str, src: Path, dst: Path, fmt: str, ctx=None) -> Path:
    """Convert via Microsoft Office (`kind` = word | excel | powerpoint), else LibreOffice.

    Raises OfficeError if neither works.
    """
    errors: list[str] = []
    office = {"word": (word_available, word_convert), "excel": (excel_available, excel_convert),
              "powerpoint": (powerpoint_available, None)}[kind]
    if office[0]():
        try:
            if kind == "powerpoint":
                if fmt != "pdf":
                    raise OfficeError(tr("PowerPoint can only convert to PDF here"))
                return powerpoint_to_pdf(src, dst)
            return office[1](src, dst, fmt)
        except OfficeError as e:
            log.warning("Office conversion failed: %s", e)
            errors.append(str(e))
    if libreoffice_available():
        tmp = Path(dst).parent / f".satsuma-lo-{uuid.uuid4().hex[:8]}"
        try:
            produced = soffice_convert(src, tmp, fmt, ctx)
            shutil.move(str(produced), str(dst))
            return Path(dst)
        except (OfficeError, UserError) as e:
            if str(e) in ("Cancelled", tr("Cancelled")) or getattr(ctx, "cancelled", False):
                raise
            errors.append(str(e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    raise OfficeError("; ".join(errors) or tr("Needs Microsoft Office or LibreOffice"))
