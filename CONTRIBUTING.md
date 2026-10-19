# Contributing to Satsuma

Thanks for helping! Bug reports, fixes, new converters and translations are all welcome.

## Set up

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m satsuma
```

## Adding a converter, tool or action

Everything the wheel shows is an `Option` registered in a module under
`satsuma/converters/`, `satsuma/tools/` or `satsuma/actions/` — read the docstring in
`satsuma/registry.py`. Add new modules to `PLUGIN_MODULES` there (a test checks it).
Work runs on a worker thread; dialogs (`satsuma/ui/common.py` → `ToolDialog`) run on
the GUI thread. Add tests next to the existing ones in `tests/`.

## Text and translations

All user-visible text is English in the code, wrapped in `tr()` (or `N_()` for tables
built at import time), with named placeholders: `tr("Moved {n} items").format(n=3)`.
Never pass f-strings to `tr()` or build sentences from pieces.

After changing text:

```powershell
.\.venv\Scripts\python.exe scripts\extract_strings.py          # add/remove keys in every catalog
.\.venv\Scripts\python.exe scripts\extract_strings.py --check  # what's untranslated
```

### Adding a language

1. Add the code and native name to `LANGUAGES` in `satsuma/i18n.py` (and to
   `RTL_LANGUAGES` for right-to-left scripts).
2. Add the code to `LANGS` in `scripts/extract_strings.py`, run it, and translate
   `satsuma/locales/<code>.json`. `scripts/strings_context.json` shows where each
   string is used; wheel labels (`scripts/wedge_labels.json`) must stay short.
3. Run the tests: they check placeholders and that every wheel label fits.

## License

By contributing you agree that your contribution is licensed under the
AGPL-3.0-or-later, like the rest of the project.
