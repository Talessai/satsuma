"""Collect translatable strings and sync satsuma/locales/<lang>.json.

    python scripts/extract_strings.py          # add new keys (empty), drop obsolete ones
    python scripts/extract_strings.py --check  # exit 1 if anything is untranslated or broken

Collected: string literals passed to tr() / N_(), and literal `label=` /
`description=` arguments of Option(...) / register(...) calls (shown via
Option.title / Option.text). Labels that are file-format names (all caps/digits,
e.g. "PNG", "MP3") are left out: they are never translated.
"""
from __future__ import annotations

import ast
import json
import re
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "satsuma"
LOCALES = PKG / "locales"
LANGS = ("zh", "hi", "es", "ar")
FORMAT_NAME = re.compile(r"^[A-Z0-9 .+/-]+$")


def _const(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def collect() -> tuple[dict[str, set[str]], list[str]]:
    """{key: {file:line, ...}}, [problems]"""
    keys: dict[str, set[str]] = {}
    problems: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None)
            where = f"{rel}:{node.lineno}"
            if fn in ("tr", "N_") and node.args:
                arg = node.args[0]
                text = _const(arg)
                if text is not None:
                    keys.setdefault(text, set()).add(where)
                elif isinstance(arg, ast.JoinedStr):
                    problems.append(f"{where}: f-string passed to {fn}()")
            if fn in ("Option", "register"):
                for kw in node.keywords:
                    if kw.arg in ("label", "description"):
                        text = _const(kw.value)
                        if text and not (kw.arg == "label" and FORMAT_NAME.match(text)):
                            keys.setdefault(text, set()).add(where)
    return keys, problems


def placeholders(s: str) -> set[str]:
    try:
        return {f for _, f, _, _ in string.Formatter().parse(s) if f is not None}
    except ValueError:
        return {"<invalid format string>"}


def main(check: bool) -> int:
    keys, problems = collect()
    LOCALES.mkdir(exist_ok=True)
    status = 0
    if not check:
        # Where each string is used, for translators (not shipped with the app).
        ctx = {k: sorted(v) for k, v in sorted(keys.items())}
        (ROOT / "scripts" / "strings_context.json").write_text(
            json.dumps(ctx, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for lang in LANGS:
        path = LOCALES / f"{lang}.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        new = {k: old.get(k, "") for k in sorted(keys)}
        missing = [k for k, v in new.items() if not v]
        bad = [k for k, v in new.items() if v and placeholders(v) != placeholders(k)]
        if not check:
            path.write_text(json.dumps(new, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"{lang}: {len(new)} keys, {len(missing)} untranslated, {len(bad)} placeholder mismatches")
        for k in bad:
            problems.append(f"{lang}: placeholders differ for {k!r}: {new[k]!r}")
        if check and missing:
            problems.extend(f"{lang}: untranslated {k!r}" for k in missing[:10])
    for p in problems:
        print("  " + p)
    if problems:
        status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main("--check" in sys.argv))
