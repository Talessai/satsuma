"""Registry of everything the wheels can offer.

Every converter, tool and action registers an `Option`. The wheel asks
`options_for(mode, paths)` which options apply to the dragged selection,
and the job manager runs the chosen one.

How an option runs (see jobs.JobManager):
  1. If `dialog` is set it is called on the GUI thread as
     `dialog(paths, ctx) -> dict | None`. Returning None cancels. The dict
     becomes `ctx.params`. A dialog may do all the work itself and return
     {"done": True, "outputs": [...]} - then `run` is skipped.
  2. `run` is called on a worker thread.
       multi=False: `run(src: Path, ctx) -> Path | list[Path] | None`, once per file.
       multi=True:  `run(paths: list[Path], ctx) -> Path | list[Path] | None`, once.
     Raise `UserError("message")` for problems the user should see without a traceback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import formats


class UserError(Exception):
    """An error with a message meant for the user (shown in the toast)."""


@dataclass
class Option:
    id: str
    label: str                      # short wedge label, e.g. "PNG" or "Compress"
    mode: str                       # "convert" | "tools" | "actions"
    run: Callable | None = None
    dialog: Callable | None = None
    exts: frozenset[str] | None = None   # accepted input extensions; None = any file
    allow_dirs: bool = False
    files_only: bool = True         # reject folders unless allow_dirs
    min_files: int = 1
    max_files: int | None = None
    multi: bool = False
    family: str = "any"
    order: int = 100
    description: str = ""
    skip_same_ext: str | None = None  # hide when every input already has this extension
    accepts: Callable[[list[Path]], bool] | None = None  # extra custom predicate
    # Placeholders for `description`, which is an English template translated at display
    # time, e.g. description="Convert images to {fmt}", desc_args={"fmt": "PNG"}.
    desc_args: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        """Label in the UI language (format names like 'PNG' have no translation)."""
        from .i18n import tr

        return tr(self.label)

    @property
    def text(self) -> str:
        """Description in the UI language."""
        from .i18n import tr

        return tr(self.description).format(**self.desc_args) if self.description else ""

    def applies(self, paths: list[Path]) -> bool:
        if not paths or len(paths) < self.min_files:
            return False
        if self.max_files is not None and len(paths) > self.max_files:
            return False
        for p in paths:
            if p.is_dir():
                if not self.allow_dirs:
                    return False
                continue
            if self.exts is not None and formats.ext(p) not in self.exts:
                return False
        if self.skip_same_ext and all(
            not p.is_dir() and formats.same_format(formats.ext(p), self.skip_same_ext) for p in paths
        ):
            return False
        if self.accepts is not None and not self.accepts(paths):
            return False
        return True


_options: dict[str, Option] = {}


def add(option: Option) -> Option:
    if option.id in _options:
        raise ValueError(f"duplicate option id {option.id}")
    _options[option.id] = option
    return option


def register(id: str, label: str, mode: str, *, exts: Iterable[str] | None = None, **kw):
    """Decorator form: the decorated function becomes the option's `run`."""

    def deco(fn):
        add(Option(id=id, label=label, mode=mode, run=fn,
                   exts=frozenset(exts) if exts is not None else None, **kw))
        return fn

    return deco


def get(option_id: str) -> Option:
    return _options[option_id]


def all_options() -> list[Option]:
    return list(_options.values())


def options_for(mode: str, paths: list[Path]) -> list[Option]:
    found = [o for o in _options.values() if o.mode == mode and o.applies(paths)]
    found.sort(key=lambda o: (o.order, o.label))
    return found


_loaded = False
load_errors: dict[str, str] = {}  # module -> error, for modules that failed to import

# Listed explicitly because pkgutil can't enumerate modules inside a PyInstaller build.
# tests/test_core.py checks this matches the files on disk.
PLUGIN_MODULES = (
    "converters.images", "converters.media", "converters.pdf", "converters.documents",
    "converters.slides", "converters.sheets", "converters.subtitles", "converters.archives",
    "tools.image_basic", "tools.image_edit", "tools.image_annotate", "tools.image_background",
    "tools.image_redact", "tools.media_tools", "tools.pdf_tools",
    "actions.file_actions", "actions.shelf",
)


def load_all() -> None:
    """Import every module that registers options."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    import importlib
    import logging

    for name in PLUGIN_MODULES:
        try:
            importlib.import_module(f"{__package__}.{name}")
        except Exception as e:  # one broken module must not take down the app
            logging.exception("failed to load %s", name)
            load_errors[name] = f"{type(e).__name__}: {e}"
