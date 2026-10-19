"""Build the Satsuma download website.

    python site/build.py                      # site/template.html + i18n/*.json -> site/public/
    python site/build.py --out DIR            # build somewhere else (used by tests)

Only the Python standard library is needed (CI runs this on plain Linux Python). Images are
committed under site/assets/img and just copied; to refresh them from the app, run
site/make_images.py locally (needs the app's venv with Pillow).

Everything a maintainer is expected to change lives in site/config.json:
version, download size, site/repo/donate URLs, author and year. Empty URLs are
handled: no repo -> no GitHub links and a "coming soon" download note; no donate URL ->
no donate buttons; no site URL -> relative links and no sitemap.xml.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "public"
MARKER = ".satsuma-site"

# code, native name, Open Graph locale, text direction
LANGS = [
    ("en", "English", "en_US", "ltr"),
    ("zh", "中文（简体）", "zh_CN", "ltr"),
    ("hi", "हिन्दी", "hi_IN", "ltr"),
    ("es", "Español", "es_ES", "ltr"),
    ("ar", "العربية", "ar_AR", "rtl"),
]
HREFLANG = {"en": "en", "zh": "zh-Hans", "hi": "hi", "es": "es", "ar": "ar"}

# Conversion targets per family, language-neutral (63 in total, matching the app).
FAMILIES = [
    ("images", "JPG PNG WebP HEIC TIFF AVIF BMP ICO GIF SVG", "JPG PNG WebP HEIC TIFF AVIF BMP ICO PDF"),
    ("audio", "MP3 M4A WAV FLAC OGG Opus AIFF WMA", "MP3 M4A WAV FLAC OGG Opus AIFF WMA"),
    ("video", "MP4 MOV MKV WebM AVI WMV", "MP4 MOV MKV WebM AVI WMV GIF MP3"),
    ("pdf", "PDF", "DOCX PPTX XLSX JPG PNG EPUB TXT MD HTML"),
    ("documents", "DOCX DOC ODT MD TXT RTF RTFD EPUB HTML PAGES", "DOCX PDF MD TXT EPUB HTML ODT RTF PNG"),
    ("slides", "PPT PPTX ODP", "PDF JPG PNG"),
    ("sheets", "XLSX XLS XLSM ODS CSV TSV", "XLSX ODS CSV TSV PDF HTML"),
    ("subtitles", "SRT VTT", "SRT VTT TXT"),
    ("archives", "ZIP TAR GZ TGZ BZ2 XZ RAR 7Z", "ZIP TAR TGZ"),
]
TOOL_GROUPS = ["image", "video", "audio", "pdf"]
COUNTS = {"convert": 63, "tools": 34, "actions": 9}


# --------------------------------------------------------------------------- template
class Markup(str):
    """Trusted HTML: inserted as-is by the template engine."""


TOKEN = re.compile(r"({{.*?}}|{%.*?%})", re.S)


def _lookup(ctx: dict, path: str):
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)) and part.isdigit():
            cur = cur[int(part)]
        else:
            cur = getattr(cur, part, None)
        if cur is None:
            return None
    return cur


def _truthy(ctx: dict, expr: str) -> bool:
    expr = expr.strip()
    if expr.startswith("not "):
        return not _truthy(ctx, expr[4:])
    return bool(_lookup(ctx, expr))


def _parse(tokens: list[str], i: int, stop: tuple[str, ...]):
    """Return (nodes, index, stop_tag) for tokens[i:] up to one of `stop`."""
    nodes: list = []
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("{%"):
            body = tok[2:-2].strip()
            word = body.split()[0]
            if word in stop:
                return nodes, i, body
            if word == "if":
                then, i, tag = _parse(tokens, i + 1, ("else", "endif"))
                other: list = []
                if tag == "else":
                    other, i, tag = _parse(tokens, i + 1, ("endif",))
                nodes.append(("if", body[3:], then, other))
            elif word == "for":
                m = re.fullmatch(r"for\s+(\w+)\s+in\s+([\w.]+)", body)
                if not m:
                    raise SyntaxError(f"bad for: {body}")
                inner, i, _ = _parse(tokens, i + 1, ("endfor",))
                nodes.append(("for", m.group(1), m.group(2), inner))
            else:
                raise SyntaxError(f"unknown tag: {body}")
        elif tok.startswith("{{"):
            nodes.append(("var", tok[2:-2].strip()))
        else:
            nodes.append(("text", tok))
        i += 1
    if stop:
        raise SyntaxError(f"missing {stop}")
    return nodes, i, None


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def _render(nodes: list, ctx: dict) -> str:
    out = []
    for node in nodes:
        kind = node[0]
        if kind == "text":
            out.append(node[1])
        elif kind == "var":
            expr, _, filt = node[1].partition("|")
            val = _lookup(ctx, expr.strip())
            if val is None:
                raise KeyError(f"template variable not found: {expr.strip()}")
            filt = filt.strip()
            if filt == "attr":
                out.append(html.escape(strip_tags(str(val)), quote=True))
            elif filt == "json":
                out.append(json.dumps(val, ensure_ascii=False))
            elif isinstance(val, Markup):
                out.append(val)
            else:
                out.append(html.escape(str(val), quote=True))
        elif kind == "if":
            out.append(_render(node[2] if _truthy(ctx, node[1]) else node[3], ctx))
        elif kind == "for":
            for item in _lookup(ctx, node[2]) or []:
                out.append(_render(node[3], {**ctx, node[1]: item}))
    return "".join(out)


def render(template: str, ctx: dict) -> str:
    nodes, _, _ = _parse(TOKEN.split(template), 0, ())
    return _render(nodes, ctx)


# --------------------------------------------------------------------------- i18n
def flatten(d, prefix=""):
    """Dotted keys of a nested i18n structure (lists contribute index keys)."""
    keys = set()
    if isinstance(d, dict):
        for k, v in d.items():
            keys |= flatten(v, f"{prefix}{k}.")
    elif isinstance(d, list):
        for n, v in enumerate(d):
            keys |= flatten(v, f"{prefix}{n}.")
    else:
        keys.add(prefix[:-1])
    return keys


def load_strings(code: str) -> dict:
    return json.loads((HERE / "i18n" / f"{code}.json").read_text(encoding="utf-8"))


def bdi(text: str) -> str:
    return f'<bdi dir="ltr">{html.escape(text)}</bdi>'


def fill(obj, tokens: dict):
    """Turn every i18n string into Markup, expanding {placeholders}."""
    if isinstance(obj, dict):
        return {k: fill(v, tokens) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fill(v, tokens) for v in obj]
    if isinstance(obj, str):
        return Markup(re.sub(r"\{(\w+)\}", lambda m: tokens.get(m.group(1), m.group(0)), obj))
    return obj


# --------------------------------------------------------------------------- images
def picture(src_base: str, alt: str, w: int, h: int, root: str, *, eager=False, sizes="") -> Markup:
    """<picture> with 1x/2x WebP and a 1x PNG fallback, made by --screens."""
    p = f"{root}assets/img/{src_base}"
    sizes = sizes or f"(max-width: {w + 32}px) calc(100vw - 32px), {w}px"
    load = 'fetchpriority="high"' if eager else 'loading="lazy"'
    return Markup(
        f'<picture><source type="image/webp" srcset="{p}.webp {w}w, {p}@2x.webp {w * 2}w" sizes="{sizes}">'
        f'<img src="{p}.png" width="{w}" height="{h}" alt="{html.escape(strip_tags(alt), quote=True)}" '
        f'decoding="async" {load}></picture>'
    )


# --------------------------------------------------------------------------- build
def csp_hash(script: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode() + "'"


SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)


def add_csp(page: str) -> str:
    hashes = " ".join(sorted({csp_hash(s) for s in SCRIPT_RE.findall(page)}))
    policy = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        f"script-src {hashes or chr(39) + 'none' + chr(39)}; "
        "object-src 'none'; base-uri 'none'; form-action 'none'"
    )
    return page.replace("__CSP__", policy)


def page_ctx(code: str, cfg: dict, strings: dict, *, is404: bool = False) -> dict:
    site = cfg.get("site_url", "").strip().rstrip("/")
    repo = cfg.get("repo_url", "").strip().rstrip("/")
    donate = cfg.get("donate_url", "").strip()
    native, og_locale, direction = next((n, o, d) for c, n, o, d in LANGS if c == code)

    # Paths: language pages use relative URLs so the site works under any base path
    # (e.g. user.github.io/satsuma/). 404.html can be served at any depth, so it uses the
    # absolute site URL when known.
    if is404:
        root = f"{site}/" if site else "/"
    else:
        root = "" if code == "en" else "../"

    def lang_path(c: str) -> str:
        return "" if c == "en" else f"{c}/"

    def abs_url(c: str) -> str:
        return f"{site}/{lang_path(c)}" if site else ""

    def href_for(c: str) -> str:
        return f"{root}{lang_path(c)}" or "./"

    size = str(cfg.get("download_size_mb", ""))
    keys = {k: f'<kbd class="key" dir="ltr">{k}</kbd>' for k in ("Shift", "Ctrl", "Alt", "Esc")}
    tokens = {
        "version": bdi(str(cfg.get("version", ""))),
        "year": bdi(str(cfg.get("year", ""))),
        "author": html.escape(str(cfg.get("author", ""))),
        "size": bdi(size),
        "convert_count": bdi(str(COUNTS["convert"])),
        "tools_count": bdi(str(COUNTS["tools"])),
        "actions_count": bdi(str(COUNTS["actions"])),
        "shift": keys["Shift"], "ctrl": keys["Ctrl"], "alt": keys["Alt"], "esc": keys["Esc"],
        "appdata": '<code dir="ltr">%APPDATA%\\Satsuma</code>',
        "app": "Satsuma",
    }
    t = fill(strings, tokens)

    families = []
    for key, src, dst in FAMILIES:
        fam = t["inside"]["families"][key]
        families.append({"key": key, "name": fam["name"], "note": fam["note"],
                         "src": src.split(), "dst": dst.split()})
    tools = [{"key": g, "title": t["inside"]["tools"][g]["title"], "items": t["inside"]["tools"][g]["items"]}
             for g in TOOL_GROUPS]

    def pic(base: str, alt: str, w: int, h: int, **kw) -> Markup:
        return picture(f"{code}-{base}", alt, w, h, root, **kw)

    ctx = {
        "t": t,
        "c": cfg,
        "v": {
            "lang": code, "hreflang": HREFLANG[code], "dir": direction, "native": native,
            "og_locale": og_locale, "root": root, "is404": is404, "home": root if is404 else "./", "is_root": code == "en" and not is404,
            "site": site, "canonical": abs_url(code),
            "og_image": (f"{site}/" if site else root) + f"assets/img/og-{code}.png",
            "has_repo": bool(repo), "repo_url": repo,
            "issues_url": f"{repo}/issues" if repo else "",
            "translate_url": f"{repo}/tree/main/satsuma/locales" if repo else "",
            "license_url": f"{repo}/blob/main/LICENSE" if repo else "https://www.gnu.org/licenses/agpl-3.0.html",
            "download_url": f"{repo}/releases/latest/download/Satsuma-Setup.exe" if repo else "#download",
            "zip_url": f"{repo}/releases/latest/download/Satsuma-win64.zip" if repo else "",
            "releases_url": f"{repo}/releases" if repo else "",
            "has_donate": bool(donate), "donate_url": donate,
            "langs": [{"code": c, "name": n, "href": href_for(c), "hreflang": HREFLANG[c], "dir": d,
                       "current": c == code} for c, n, _, d in LANGS],
            "alternates": [{"hreflang": HREFLANG[c], "href": abs_url(c) or href_for(c)} for c, *_ in LANGS],
            "x_default": abs_url("en") or href_for("en"),
            "families": families, "tools": tools,
            "pic_hero": pic("wheel-convert", t["img"]["wheel_convert"], 388, 388, eager=True,
                            sizes="(max-width: 420px) calc(100vw - 32px), 388px"),
            "pic_convert": pic("wheel-convert", t["img"]["wheel_convert"], 388, 388),
            "pic_tools": pic("wheel-tools", t["img"]["wheel_tools"], 388, 388),
            "pic_actions": pic("wheel-actions", t["img"]["wheel_actions"], 388, 388),
            "pic_welcome": pic("welcome", t["img"]["welcome"], 480, 364),
        },
    }
    return ctx


def check_keys(langs: dict[str, dict]) -> None:
    ref = flatten(langs["en"])
    for code, s in langs.items():
        got = flatten(s)
        if got != ref:
            missing, extra = sorted(ref - got), sorted(got - ref)
            raise SystemExit(f"i18n/{code}.json keys differ from en.json: missing={missing} extra={extra}")


def prepare_out(out: Path) -> None:
    if out.exists() and any(out.iterdir()):
        if not (out / MARKER).exists():
            raise SystemExit(f"refusing to clean {out}: not a Satsuma site build folder (no {MARKER})")
        for child in out.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("generated by site/build.py\n", encoding="utf-8", newline="\n")


def build(out: Path = DEFAULT_OUT, cfg: dict | None = None) -> list[Path]:
    """Write the site into `out`. `cfg` overrides site/config.json (used by tests)."""
    if cfg is None:
        cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    template = (HERE / "template.html").read_text(encoding="utf-8")
    strings = {c: load_strings(c) for c, *_ in LANGS}
    check_keys(strings)

    out = Path(out)
    prepare_out(out)
    shutil.copytree(HERE / "assets", out / "assets")
    (out / ".nojekyll").write_text("", encoding="utf-8", newline="\n")

    written = []
    for code, *_ in LANGS:
        dest = out / ("index.html" if code == "en" else f"{code}/index.html")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(add_csp(render(template, page_ctx(code, cfg, strings[code]))), encoding="utf-8", newline="\n")
        written.append(dest)

    nf = out / "404.html"
    nf.write_text(add_csp(render(template, page_ctx("en", cfg, strings["en"], is404=True))), encoding="utf-8", newline="\n")
    written.append(nf)

    site = cfg.get("site_url", "").strip().rstrip("/")
    robots = "User-agent: *\nAllow: /\n"
    if site:
        robots += f"\nSitemap: {site}/sitemap.xml\n"
        alts = "".join(
            f'\n    <xhtml:link rel="alternate" hreflang="{HREFLANG[c]}" href="{site}/{"" if c == "en" else c + "/"}"/>'
            for c, *_ in LANGS
        ) + f'\n    <xhtml:link rel="alternate" hreflang="x-default" href="{site}/"/>'
        urls = "".join(
            f'\n  <url>\n    <loc>{site}/{"" if c == "en" else c + "/"}</loc>{alts}\n  </url>' for c, *_ in LANGS
        )
        (out / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            f'xmlns:xhtml="http://www.w3.org/1999/xhtml">{urls}\n</urlset>\n',
            encoding="utf-8", newline="\n",
        )
        written.append(out / "sitemap.xml")
    (out / "robots.txt").write_text(robots, encoding="utf-8", newline="\n")
    written.append(out / "robots.txt")
    return written


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder (default: site/public)")
    args = ap.parse_args(argv)
    files = build(args.out)
    print(f"built {len(files)} files into {args.out}")


if __name__ == "__main__":
    sys.exit(main())
