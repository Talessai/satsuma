"""Archives: extract (ZIP TAR GZ TGZ BZ2 XZ RAR 7Z), convert ZIP/TAR/TGZ, and pack anything."""
from __future__ import annotations

import bz2
import gzip
import lzma
import os
import shutil
import tarfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from .. import formats, proc
from ..i18n import N_, tr
from ..paths import safe_filename, unique_path
from ..registry import Option, UserError, add

PACK_EXT = {"zip": "zip", "tar": "tar", "tgz": "tar.gz"}
DEFAULT_ARCHIVE_NAME = N_("Archive")  # name of an archive packed from several items
_COMPRESSORS = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}
_MULTI_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz")


def archive_stem(path: Path) -> str:
    """'photos.tar.gz' -> 'photos', 'notes.zip' -> 'notes'."""
    name = path.name
    low = name.lower()
    for suf in _MULTI_SUFFIXES:
        if low.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return path.stem or name


def tar_exe() -> str:
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
    if exe.exists():
        return str(exe)
    found = shutil.which("tar")
    if found:
        return found
    raise UserError(tr("Extracting this archive needs tar.exe (Windows 10 1803 or newer)"))


# --------------------------------------------------------------------------- safe extraction


def safe_member(dest: Path, name: str) -> Path | None:
    """Target path for an archive member, or None if it would escape `dest` (zip-slip)."""
    name = name.replace("\\", "/")
    p = PurePosixPath(name)
    if not name or not p.parts or p.is_absolute():
        return None
    if any(part == ".." or ":" in part for part in p.parts):
        return None
    target = (dest / Path(*[q for q in p.parts if q not in ("", ".")])).resolve()
    root = dest.resolve()
    if target != root and root not in target.parents:
        return None
    return target


def extract_zip(src: Path, dest: Path, ctx=None) -> int:
    """Extract a ZIP safely; returns the number of skipped (unsafe) entries."""
    skipped = 0
    with zipfile.ZipFile(src) as z:
        infos = z.infolist()
        for i, info in enumerate(infos):
            if ctx is not None:
                ctx.check_cancel()
                if i % 20 == 0:
                    ctx.progress(i / max(len(infos), 1))
            target = safe_member(dest, info.filename)
            if target is None:
                skipped += 1
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with z.open(info) as fsrc, open(target, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst, 1 << 20)
            except RuntimeError as e:  # encrypted entries
                raise UserError(tr("{name} is password protected").format(name=src.name)) from e
    return skipped


def extract_tar(src: Path, dest: Path, ctx=None) -> int:
    skipped = 0
    with tarfile.open(src, "r:*") as t:
        safe = []
        for m in t.getmembers():
            if safe_member(dest, m.name) is None or m.isdev() or (
                    (m.issym() or m.islnk()) and safe_member(dest, str(PurePosixPath(m.name).parent / m.linkname)) is None):
                skipped += 1
                continue
            safe.append(m)
        for i, m in enumerate(safe):
            if ctx is not None:
                ctx.check_cancel()
                if i % 20 == 0:
                    ctx.progress(i / max(len(safe), 1))
            try:
                t.extract(m, dest, filter="data")
            except (tarfile.FilterError, OSError):
                skipped += 1
    return skipped


def extract_any(src: Path, dest: Path, ctx=None) -> int:
    """Extract `src` into `dest` (created). Returns the number of skipped unsafe entries."""
    dest.mkdir(parents=True, exist_ok=True)
    kind = formats.ext(src)
    if kind == "zip" or (kind not in ("rar", "7z") and zipfile.is_zipfile(src)):
        return extract_zip(src, dest, ctx)
    if kind in ("tar", "tgz") or (kind in _COMPRESSORS and _is_tar(src)):
        return extract_tar(src, dest, ctx)
    if kind in _COMPRESSORS:
        out = dest / (src.stem or "file")
        with _COMPRESSORS[kind](src, "rb") as fsrc, open(out, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, 1 << 20)
        return 0
    if kind in ("rar", "7z"):
        # bsdtar refuses absolute paths and '..' entries unless -P is given.
        if ctx is not None:
            ctx.progress(None, tr("Extracting…"))
        proc.run([tar_exe(), "-xf", str(src.resolve()), "-C", str(dest.resolve())], ctx)
        return _remove_escapes(dest)
    raise UserError(tr("Unsupported archive: {name}").format(name=src.name))


def _is_tar(src: Path) -> bool:
    try:
        return tarfile.is_tarfile(src)
    except Exception:
        return False


def _remove_escapes(dest: Path) -> int:
    """Defence in depth after an external extractor: drop links pointing outside dest."""
    n = 0
    root = dest.resolve()
    for p in dest.rglob("*"):
        if p.is_symlink():
            try:
                target = p.resolve()
            except OSError:
                target = None
            if target is None or (target != root and root not in target.parents):
                p.unlink()
                n += 1
    return n


def extract_archive(src: Path, ctx) -> Path:
    """Extract into a folder named after the archive (or the single item it contains)."""
    src = Path(src)
    stem = archive_stem(src)
    parent = ctx.output_path(src, "", name=stem).parent
    work = parent / f".{stem}.satsuma-{uuid.uuid4().hex[:6]}"
    try:
        try:
            skipped = extract_any(src, work, ctx)
        except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, lzma.LZMAError) as e:
            raise UserError(_damaged(src, e)) from e
        entries = list(work.iterdir())
        if not entries:
            raise UserError(tr("{name} is empty").format(name=src.name))
        if len(entries) == 1:
            final = unique_path(parent / entries[0].name)
            shutil.move(str(entries[0]), str(final))
        else:
            final = ctx.output_path(src, "", name=stem)
            work.rename(final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if skipped:
        ctx.progress(1.0, _skipped(skipped))
    return final


def _damaged(src: Path, e: Exception) -> str:
    return tr("{name} is damaged or not a supported archive: {error}").format(name=src.name, error=e)


def _skipped(n: int) -> str:
    text = tr("Skipped 1 unsafe entry") if n == 1 else tr("Skipped {n} unsafe entries")
    return text.format(n=n)


# --------------------------------------------------------------------------- packing


def _walk(item: Path):
    """(path, arcname) pairs for a file or a folder tree, folders included."""
    base = item.parent
    yield item, item.relative_to(base).as_posix()
    if item.is_dir():
        for root, dirs, files in os.walk(item):
            dirs.sort()
            for name in sorted(dirs) + sorted(files):
                p = Path(root) / name
                yield p, p.relative_to(base).as_posix()


def pack(items: list[Path], dst: Path, kind: str, ctx=None, skip: Path | None = None) -> Path:
    """Write `items` (files and folders) into a new zip / tar / tgz archive at `dst`."""
    entries = [(p, a) for it in items for p, a in _walk(Path(it))]
    skip_r = skip.resolve() if skip else None
    tmp = dst.with_name(dst.name + ".part")
    total = max(len(entries), 1)
    try:
        if kind == "zip":
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                for i, (p, arc) in enumerate(entries):
                    _tick(ctx, i, total)
                    if skip_r and p.resolve() == skip_r:
                        continue
                    if p.is_dir():
                        z.write(p, arc + "/")
                    else:
                        z.write(p, arc)
        else:
            mode = "w:gz" if kind == "tgz" else "w"
            with tarfile.open(tmp, mode) as t:
                for i, (p, arc) in enumerate(entries):
                    _tick(ctx, i, total)
                    if skip_r and p.resolve() == skip_r:
                        continue
                    t.add(p, arc, recursive=False)
        tmp.replace(dst)
    finally:
        if tmp.exists():
            tmp.unlink()
    return dst


def _tick(ctx, i: int, total: int) -> None:
    if ctx is not None:
        ctx.check_cancel()
        if i % 25 == 0:
            ctx.progress(i / total)


def _make_convert(target: str):
    def run(src: Path, ctx) -> Path:
        src = Path(src)
        work = ctx.tmpdir / f"x-{uuid.uuid4().hex[:6]}"
        try:
            skipped = extract_any(src, work, ctx)
        except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, lzma.LZMAError) as e:
            raise UserError(_damaged(src, e)) from e
        dst = ctx.output_path(src, PACK_EXT[target], name=archive_stem(src))
        pack(sorted(work.iterdir()), dst, target, ctx)
        if skipped:
            ctx.progress(1.0, _skipped(skipped))
        return dst

    run.__name__ = f"archive_to_{target}"
    return run


def _make_pack(target: str):
    def run(paths: list[Path], ctx) -> Path:
        paths = [Path(p) for p in paths]
        if len(paths) == 1:
            p = paths[0]
            dst = ctx.output_path(p, PACK_EXT[target], name=p.name if p.is_dir() else p.stem)
        else:
            dst = ctx.output_path(paths[0], PACK_EXT[target], name=safe_filename(tr(DEFAULT_ARCHIVE_NAME), "Archive"))
        return pack(paths, dst, target, ctx, skip=dst)

    run.__name__ = f"pack_{target}"
    return run


def _not_all_archives(paths: list[Path]) -> bool:
    if all(p.is_file() and formats.ext(p) in formats.ARCHIVE_IN for p in paths):
        return False
    # A single known file (photo, PDF...) has real conversions; don't crowd its wheel with
    # packing options. Folders, multi-selections and unknown file types get them.
    if len(paths) == 1 and paths[0].is_file() and formats.family(paths[0]) is not None:
        return False
    return True


add(Option(id="archive.extract", label="Extract", mode="convert", run=extract_archive,
           exts=frozenset(formats.ARCHIVE_IN), family="archive", order=10,
           description="Extract the archive into a folder"))

for _i, _t in enumerate(("zip", "tar", "tgz")):
    add(Option(id=f"archive.to.{_t}", label=_t.upper(), mode="convert", run=_make_convert(_t),
               exts=frozenset(formats.ARCHIVE_IN), family="archive", order=20 + _i, skip_same_ext=_t,
               description="Repack the archive as {fmt}", desc_args={"fmt": _t.upper()}))
    add(Option(id=f"any.to.{_t}", label=_t.upper(), mode="convert", run=_make_pack(_t), exts=None,
               allow_dirs=True, files_only=False, multi=True, family="archive", order=90 + _i,
               accepts=_not_all_archives,
               description="Pack the selection into a {fmt} archive", desc_args={"fmt": _t.upper()}))
