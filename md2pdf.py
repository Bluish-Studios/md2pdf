#!/usr/bin/env python3
"""md2pdf: convert Markdown to a paginated PDF laid out for a reMarkable tablet (or A5/A4/Letter paper).

Needs only Python 3.13+ and Microsoft Edge. On first run it installs its Python packages (markdown-it-py,
mdit-py-plugins, pypdf, websockets) into a private folder next to this script, never into your Python.
Documents are rendered locally by a headless Microsoft Edge; nothing is uploaded anywhere.

Usage: md2pdf [options] INPUT...        Run with --help for the options.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import datetime as dt
import glob
import hashlib
import html
import importlib
import io
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

VERSION = "1.0.0"
REQUIREMENTS = ["markdown-it-py>=3,<5", "mdit-py-plugins>=0.4,<1", "pypdf>=4,<7", "websockets>=15,<17"]
TOOL_DIR = pathlib.Path(__file__).resolve().parent
MERMAID_URLS = ["https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js",
                "https://unpkg.com/mermaid@11/dist/mermaid.min.js"]
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_SETUP = 0, 1, 2, 3
MD_SUFFIXES = (".md", ".markdown", ".mdown", ".mkd")

# (label, page width mm, page height mm, margins top/right/bottom/left mm, body font pt). The reMarkable
# sizes are the physical screen sizes, so a page fills the screen at 1:1 and 11 pt reads as 11 pt.
DEVICES = {
    "paper-pro": ("reMarkable Paper Pro", 180.0, 240.0, (13, 11, 15, 11), 11.0),
    "rm2": ("reMarkable 2", 157.5, 210.0, (12, 10, 14, 10), 10.5),
    "move": ("reMarkable Paper Pro Move", 91.8, 163.2, (7, 5, 10, 5), 9.5),
    "a5": ("A5 paper", 148.0, 210.0, (14, 13, 16, 13), 10.0),
    "a4": ("A4 paper", 210.0, 297.0, (20, 20, 22, 20), 11.0),
    "letter": ("US Letter paper", 215.9, 279.4, (19, 19, 21, 19), 11.0),
}


class SetupError(Exception):
    """Something md2pdf needs (packages, Edge, a writable folder) is not available."""


def log(msg: str) -> None:
    print(f"md2pdf: {msg}", file=sys.stderr, flush=True)


def writable_dir(path: pathlib.Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".probe-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def runtime_home() -> pathlib.Path:
    """Where packages, caches and temp files live: MD2PDF_HOME, else .runtime next to this script."""
    if os.environ.get("MD2PDF_HOME"):
        home = pathlib.Path(os.environ["MD2PDF_HOME"]).expanduser().resolve()
        if not writable_dir(home):
            raise SetupError(f"MD2PDF_HOME is not writable: {home}")
        return home
    home = TOOL_DIR / ".runtime"
    if writable_dir(home):
        return home
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or str(pathlib.Path.home() / ".cache")
    home = pathlib.Path(base) / "md2pdf"
    if not writable_dir(home):
        raise SetupError(f"no writable folder for the runtime (tried {TOOL_DIR / '.runtime'} and {home})")
    return home


def ensure_packages(home: pathlib.Path) -> None:
    """Install the packages into a private folder (once per Python version) and put it first on sys.path."""
    key = hashlib.sha1("\n".join(REQUIREMENTS).encode()).hexdigest()[:8]  # noqa: S324 - a folder name, not security
    tag = f"lib-py{sys.version_info[0]}{sys.version_info[1]}"
    lib, done = home / f"{tag}-{key}", home / f"{tag}-{key}" / ".complete"
    if not done.exists():
        shutil.rmtree(lib, ignore_errors=True)
        log(f"first run for this Python: installing {len(REQUIREMENTS)} packages into {lib}")
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--quiet",
               "--no-warn-script-location", "--target", str(lib), *REQUIREMENTS]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,  # noqa: S603 - fixed pip command
                             errors="replace")
        if res.returncode != 0:
            hint = ""
            if "No module named pip" in res.stdout:
                hint = "\nThis Python has no pip. Install pip, or on Windows set MD2PDF_PYTHON=private."
            raise SetupError(f"pip could not install {' '.join(REQUIREMENTS)}:\n{res.stdout.strip()[-3000:]}{hint}")
        done.write_text("\n".join(REQUIREMENTS), encoding="utf-8")
        for old in home.glob(f"{tag}-*"):
            if old != lib:
                shutil.rmtree(old, ignore_errors=True)
    sys.path.insert(0, str(lib))
    importlib.invalidate_caches()
    try:
        load_deps()
    except ImportError as e:
        done.unlink(missing_ok=True)
        raise SetupError(f"the installed packages failed to import ({e}); run md2pdf again to reinstall them") from e


def load_deps() -> None:
    global MarkdownIt, PdfReader, ws_connect, ConnectionClosed, front_matter_plugin, tasklists_plugin, footnote_plugin
    from markdown_it import MarkdownIt
    from mdit_py_plugins.footnote import footnote_plugin
    from mdit_py_plugins.front_matter import front_matter_plugin
    from mdit_py_plugins.tasklists import tasklists_plugin
    from pypdf import PdfReader
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed
    logging.getLogger("pypdf").setLevel(logging.ERROR)


def clean_stale(home: pathlib.Path) -> None:
    for old in (home / "tmp").glob("run-*"):
        with contextlib.suppress(OSError):
            if time.time() - old.stat().st_mtime > 86400:
                shutil.rmtree(old, ignore_errors=True)


def ensure_mermaid(home: pathlib.Path) -> tuple[pathlib.Path | None, str]:
    """mermaid.js, downloaded once and cached. It runs locally inside Edge; no document content leaves the machine."""
    dest = home / "cache" / "mermaid.min.js"
    if dest.is_file() and dest.stat().st_size > 100_000:
        return dest, "cached"
    sources = [os.environ["MD2PDF_MERMAID_URL"]] if os.environ.get("MD2PDF_MERMAID_URL") else MERMAID_URLS
    errors = []
    for src in sources:
        try:
            if os.path.isfile(src):
                data = pathlib.Path(src).read_bytes()
            else:
                log(f"downloading mermaid.js (one time) from {src}")
                with urllib.request.urlopen(src, timeout=60) as r:  # noqa: S310 - the built-in CDNs or the user's MD2PDF_MERMAID_URL
                    data = r.read()
            if len(data) < 100_000 or b"mermaid" not in data:
                raise ValueError(f"unexpected content ({len(data)} bytes)")
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            return dest, "downloaded"
        except Exception as e:  # noqa: BLE001 - report every source that failed
            errors.append(f"{src}: {e}")
    return None, "; ".join(errors)


class Layout:
    """Page geometry for one device, in the units the CSS, the code-block fitter and the browser need."""

    def __init__(self, device: str, font_pt: float | None):
        if device in DEVICES:
            self.label, self.w, self.h, margins, font = DEVICES[device]
        else:
            m = re.fullmatch(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(mm|cm|in)?", device.strip().lower())
            if not m:
                raise ValueError(f"unknown device {device!r}; use one of {', '.join(DEVICES)} or WIDTHxHEIGHT[mm|cm|in]")
            k = {"mm": 1.0, None: 1.0, "cm": 10.0, "in": 25.4}[m.group(3)]
            self.w, self.h = float(m.group(1)) * k, float(m.group(2)) * k
            if not (40 <= self.w <= 600 and 40 <= self.h <= 900):
                raise ValueError(f"page size {self.w:g} x {self.h:g} mm is out of range")
            self.label = f"{self.w:g} x {self.h:g} mm page"
            margins = (0.055 * self.h, 0.06 * self.w, 0.065 * self.h, 0.06 * self.w)
            font = 11.0 if self.w >= 150 else 10.0 if self.w >= 110 else 9.5
        self.device = device
        self.mt, self.mr, self.mb, self.ml = (float(v) for v in margins)
        self.font = float(font_pt or font)
        self.scale = self.font / 11.0
        self.content_w_mm = self.w - self.ml - self.mr
        self.content_h_mm = self.h - self.mt - self.mb
        self.content_w_pt = self.content_w_mm * 72 / 25.4
        self.content_w_px = self.content_w_mm * 96 / 25.4
        self.content_h_px = self.content_h_mm * 96 / 25.4

    def print_params(self) -> dict:
        inch = lambda mm: mm / 25.4
        return {"printBackground": True, "preferCSSPageSize": True, "displayHeaderFooter": False,
                "paperWidth": inch(self.w), "paperHeight": inch(self.h),
                "marginTop": inch(self.mt), "marginBottom": inch(self.mb),
                "marginLeft": inch(self.ml), "marginRight": inch(self.mr),
                "generateTaggedPDF": True, "generateDocumentOutline": True}


MONO_ADVANCE_EM = 0.5498            # Consolas glyph advance
PRE_CHROME_PT = 6 + 6 + 2.5 + 0.75  # code block padding and borders
NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.*)$")
REF_RE = re.compile(r"§\s?(\d+(?:\.\d+)*)")
CHAIN_SEP = re.compile(r",\s*|\s+and\s+|\s+or\s+|\s*[–-]\s*")
# A § ref right after a file name ("see design.md §4") points into that file, not into this document.
FOREIGN_DOC = re.compile(r"[\w.\-]+\.(?:md|markdown|pdf|docx?|pptx?|txt|html?)\b[^.;:!?]{0,25}$", re.I)
INLINE_RE = re.compile(r"§\s?\d+(?:\.\d+)*|https?://[^\s<>()|\"']+")
ALERT_RE = re.compile(r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*", re.I)
CHAPTER_MARK = "<!--md2pdf:chapter-->"
TOC_MARK = "<!--md2pdf:toc-->"
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})")
ADO_IMG_SIZE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^\s)>]+)>?\s+=(\d*)x(\d*)\s*\)")
# Azure DevOps wiki treats '#Heading' (no space) as a heading. C preprocessor lines and #123 work items are left alone.
ADO_HEADING = re.compile(r"^( {0,3}#{1,6})(?=[^\s#\d])(?!(?:include|define|undef|ifn?def|if|elif|else|endif|pragma|import"
                         r"|using|region|endregion|error|warning|line)\b)")


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def attr(s: str) -> str:
    return html.escape(s, quote=True)


def with_breaks(s: str, url: bool = False) -> str:
    """Escape s and add <wbr> opportunities so long paths, identifiers and URLs wrap cleanly."""
    if url:
        marked = re.sub(r"(?<=[/.\-?=&#_])", "\0", s)
    else:
        marked = re.sub(r"(?<=[/\\(,|=;_])", "\0", s)
        marked = re.sub(r"(?<=[a-z0-9])\.(?=[A-Z])", ".\0", marked)
        marked = re.sub(r"[A-Za-z0-9]{18,}", lambda m: re.sub(r"(?<=[a-z])(?=[A-Z])", "\0", m.group(0)), marked)
    return "<wbr>".join(esc(p) for p in marked.split("\0"))


def read_text(path: pathlib.Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def preprocess(text: str) -> str:
    """Azure DevOps wiki syntax -> CommonMark: :::mermaid blocks, [[_TOC_]], '#Heading', and ![alt](src =WxH) image sizes."""
    out, fence, ado = [], None, None
    for line in text.split("\n"):
        if ado is not None:
            if line.strip() == ":::":
                out.append(ado + "```")
                ado = None
            else:
                out.append(line)
            continue
        m = FENCE_RE.match(line)
        if fence:
            out.append(line)
            if m and m.group(2)[0] == fence[0] and len(m.group(2)) >= len(fence) and not line.strip().strip(fence[0]):
                fence = None
            continue
        if m:
            fence = m.group(2)
            out.append(line)
            continue
        s = line.strip()
        if re.fullmatch(r":::\s*mermaid", s, re.I):
            ado = line[:len(line) - len(line.lstrip())]
            out.append(ado + "```mermaid")
        elif re.fullmatch(r"\[\[_TOC_\]\]|\[TOC\]", s, re.I):
            out.extend(["", TOC_MARK, ""])
        elif re.fullmatch(r"\[\[_TOSP_\]\]", s, re.I):
            continue
        else:
            line = ADO_HEADING.sub(r"\1 ", line)
            out.append(ADO_IMG_SIZE.sub(lambda g: (
                f'<img src="{attr(g.group(2))}" alt="{attr(g.group(1))}"'
                + (f' width="{g.group(3)}"' if g.group(3) else "")
                + (f' height="{g.group(4)}"' if g.group(4) and not g.group(3) else "") + ">"), line))
    if ado is not None:
        out.append(ado + "```")
    return "\n".join(out)


def humanize(stem: str) -> str:
    """Azure DevOps wiki page names: 'Getting-Started' is 'Getting Started', and %2D is a literal hyphen."""
    if " " in stem or "-" not in stem:
        return urllib.parse.unquote(stem)
    return urllib.parse.unquote(stem.replace("%2D", "\0").replace("-", " ").replace("\0", "%2D"))


def slugify(text: str) -> str:
    """GitHub-style heading anchor."""
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-") or "section"


def loose(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", urllib.parse.unquote(s).lower())


def resolve_src(src: str, base: pathlib.Path) -> tuple[str | None, str]:
    """Image source -> URL Edge can load: ('file:///...', 'local'), (url, 'remote') or (None, 'missing')."""
    s = src.strip()
    if re.match(r"(?i)^(https?:|data:)", s):
        return s, "remote"
    if re.match(r"(?i)^file:", s):
        s = urllib.request.url2pathname(urllib.parse.urlparse(s).path)
    raw = s.split("#")[0].split("?")[0]
    for cand in dict.fromkeys((urllib.parse.unquote(raw), raw)):
        if not cand:
            continue
        if cand.startswith(("/", "\\")) and not re.match(r"^[\\/]{2}", cand):
            # Wiki-rooted path (/.attachments/x.png): try it against each parent folder.
            for folder in (base, *base.parents):
                p = folder / cand.lstrip("/\\")
                if p.is_file():
                    return p.resolve().as_uri(), "local"
        else:
            p = base / cand
            if p.is_file():
                return p.resolve().as_uri(), "local"
    return None, "missing"


HTML_TAGS = frozenset("""a abbr address article aside b bdi bdo big blockquote br caption center cite code col colgroup
dd del details dfn div dl dt em figcaption figure font footer h1 h2 h3 h4 h5 h6 header hr i img ins kbd li main mark
nav ol p picture pre q rp rt ruby s samp section small source span strike strong sub summary sup table tbody td tfoot
th thead time tr tt u ul var wbr svg g path rect circle ellipse line polyline polygon text tspan defs use symbol marker
lineargradient radialgradient stop title desc clippath mask pattern""".split())
DROP_TAGS = frozenset("""script style iframe object embed noscript template textarea select option button form input
link meta base frame frameset applet video audio canvas""".split())
DROP_BLOCK = re.compile(r"<(script|style|iframe|object|embed|noscript|template|textarea|select|button|form)\b.*?</\1\s*>",
                        re.I | re.S)
TAG_RE = re.compile(r"<!--.*?-->|<(/?)([A-Za-z][\w:.-]*)((?:\s+[^\s=/>\"']+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>\"']+))?)*)"
                    r"\s*(/?)>", re.S)
ATTR_RE = re.compile(r"([^\s=/>\"']+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>\"']+))?")


def sanitize_html(s: str, doc: Doc) -> str:
    """Pass real HTML through, minus scripts, forms and event handlers. Unknown <tags> are shown as text."""
    def tag(m: re.Match) -> str:
        whole = m.group(0)
        if whole.startswith("<!--"):
            return whole
        close, name, attrs, selfclose = m.groups()
        low = name.lower()
        if low == "input" and re.search(r"type\s*=\s*[\"']?checkbox", attrs, re.I):
            checked = " checked" if re.search(r"(?i)\bchecked\b", attrs) else ""
            return f'<input type="checkbox" class="task-list-item-checkbox" disabled{checked}>'
        if low in DROP_TAGS:
            return ""
        if low not in HTML_TAGS:
            return esc(whole)
        if close:
            return f"</{name}>"
        kept = []
        for am in ATTR_RE.finditer(attrs):
            name_, raw = am.group(1), am.group(2)
            key = name_.lower()
            val = html.unescape(raw[1:-1] if raw and raw[0] in "\"'" else (raw or ""))
            if key.startswith("on") or key == "srcset":
                continue
            if key in ("href", "src", "xlink:href") and re.match(r"(?i)\s*(javascript|vbscript):", val):
                continue
            if low == "img" and key == "src":
                url, _ = resolve_src(val, doc.base)
                if url is None:
                    doc.report["missing_images"].append(val)
                    return f'<span class="img-missing">[image not found: {esc(val)}]</span>'
                val = url
            elif low == "a" and key == "href":
                val = doc.link(val)[0]
                if val is None:
                    continue
            kept.append(f'{name_}="{attr(val)}"' if raw is not None else name_)
        if low == "details" and not any(k.lower().startswith("open") for k in kept):
            kept.append("open")
        return f"<{name}{''.join(' ' + k for k in kept)}{' /' if selfclose else ''}>"

    return TAG_RE.sub(tag, DROP_BLOCK.sub("", s))


def plain(inline) -> tuple[str, list[int]]:
    """Visible text of an inline token, plus the offset where each child starts."""
    parts, starts, pos = [], [], 0
    for ch in inline.children or []:
        s = ch.content if ch.type in ("text", "code_inline") else (" " if ch.type in ("softbreak", "hardbreak") else "")
        starts.append(pos)
        parts.append(s)
        pos += len(s)
    return "".join(parts), starts


def annotate_table(tokens, i: int) -> None:
    cols, in_head, j = 0, False, i + 1
    while tokens[j].type != "table_close":
        t = tokens[j]
        if t.type in ("thead_open", "thead_close"):
            in_head = t.type == "thead_open"
        elif t.type == "th_open" and in_head:
            cols += 1
        j += 1
    tokens[i].attrSet("class", f"cols-{cols}" if cols <= 8 else "cols-many")


class Doc:
    """One Markdown file: parsed tokens, headings with anchors, title, and everything the renderer resolves."""

    def __init__(self, src: pathlib.Path, layout: Layout, opts: argparse.Namespace):
        self.src, self.base, self.layout, self.opts = src, src.parent, layout, opts
        self.report = {"missing_images": [], "broken_anchors": [], "refs_linked": 0, "refs_plain": 0}
        md = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
        md.use(front_matter_plugin).use(footnote_plugin).use(tasklists_plugin)
        for name, fn in RENDER_RULES.items():
            md.add_render_rule(name, fn)
        self.md, self.env = md, {"doc": self}
        self.tokens = md.parse(preprocess(read_text(src)), self.env)
        self.mermaid_count = sum(1 for t in self.tokens if t.type == "fence" and t.info.strip().lower().split()[:1] == ["mermaid"]
                                 ) if opts.mermaid else 0
        self._prepare()

    def _prepare(self) -> None:
        toks = self.tokens
        fm = next((t.content for t in toks if t.type == "front_matter"), "")
        self.meta = {}
        for key in ("title", "author", "date", "description"):
            m = re.search(rf"(?mi)^{key}\s*:\s*(.+?)\s*$", fm)
            if m:
                self.meta[key] = m.group(1).strip().strip("\"'")
        heads = [{"level": int(t.tag[1:]), "text": plain(toks[i + 1])[0].strip(), "index": i, "nested": t.level > 0}
                 for i, t in enumerate(toks) if t.type == "heading_open"]
        h1s = [h for h in heads if h["level"] == 1]
        first = heads[0] if heads else None
        self.title_head = first if first and first["level"] == 1 and len(h1s) == 1 and not first["nested"] else None
        if self.title_head and self.meta.get("title") and loose(self.meta["title"]) != loose(self.title_head["text"]):
            self.title_head = None  # the front matter names the document; that H1 stays in the body as a heading
        self.title = self.meta.get("title") or (self.title_head["text"] if self.title_head else "") or humanize(self.src.stem)
        self.headings = [h for h in heads if h is not self.title_head]
        self.chapter_level = min((h["level"] for h in self.headings if not h["nested"]), default=None)
        self.anchors, self.loose_map, self.section_ids, used = set(), {}, {}, set()
        for h in heads:
            base_id, n = slugify(h["text"]), 1
            hid = base_id
            while hid in used:
                hid, n = f"{base_id}-{n}", n + 1
            used.add(hid)
            h["id"] = hid
            self.anchors.add(hid)
            self.loose_map.setdefault(loose(hid), hid)
            self.loose_map.setdefault(loose(h["text"]), hid)
            m = NUMBERED.match(h["text"])
            h["num"], h["label"] = (m.group(1), m.group(2)) if m else ("", h["text"])
            tok = toks[h["index"]]
            tok.attrSet("id", hid)
            if h is not self.title_head and self.chapter_level is not None:
                rel = max(0, min(h["level"] - self.chapter_level, 3))
                tok.attrSet("class", f"r{rel}")
                tok.meta = {**(tok.meta or {}), "chapter": rel == 0 and not h["nested"]}
                if m:
                    self.section_ids.setdefault(m.group(1), hid)
        for t in toks:
            for c in [t, *(t.children or [])]:
                if c.type in ("html_block", "html_inline"):
                    for a in re.findall(r"""\b(?:id|name)\s*=\s*["']([^"']+)["']""", c.content):
                        self.anchors.add(a)
                        self.loose_map.setdefault(loose(a), a)

        self.lede, self.body = [], toks
        if self.title_head:
            i = self.title_head["index"]
            end, k = i + 2, i + 3
            if k < len(toks) and toks[k].type == "blockquote_open":
                depth = 0
                for j in range(k, len(toks)):
                    depth += {"blockquote_open": 1, "blockquote_close": -1}.get(toks[j].type, 0)
                    if depth == 0:
                        end = j
                        break
            self.lede, self.body = toks[k:end + 1] if end > i + 2 else [], toks[:i] + toks[end + 1:]

        for i, tok in enumerate(toks):
            if tok.type == "table_open":
                annotate_table(toks, i)
            elif tok.type == "blockquote_open" and i + 2 < len(toks) and toks[i + 2].type == "inline":
                self._alert(tok, toks[i + 2])
            elif tok.type == "inline" and tok.children:
                self._inline(tok)

    def _alert(self, quote, inline) -> None:
        """GitHub alerts: > [!NOTE], > [!WARNING], ..."""
        first = inline.children[0] if inline.children else None
        m = ALERT_RE.match(first.content) if first is not None and first.type == "text" else None
        if not m:
            return
        kind = m.group(1).lower()
        quote.attrSet("class", f"alert alert-{kind}")
        first.content = first.content[m.end():]
        first.meta = {**(first.meta or {}), "alert": kind.capitalize()}
        if not first.content and len(inline.children) > 1 and inline.children[1].type == "softbreak":
            inline.children[1].type, inline.children[1].content = "text", ""

    def _inline(self, tok) -> None:
        text, starts = plain(tok)
        targets = self._classify_refs(text)
        stack = []
        for ch, start in zip(tok.children, starts, strict=True):
            if ch.type == "link_open":
                href, kind = self.link(str(ch.attrGet("href") or ""))
                if href is None:  # cannot be followed in a PDF: keep the text, drop the link
                    ch.tag, ch.attrs = "span", {"class": kind}
                else:
                    ch.attrSet("href", href)
                    ch.attrSet("class", kind)
                stack.append(href is None)
            elif ch.type == "link_close":
                if stack and stack.pop():
                    ch.tag = "span"
            elif ch.type == "text":
                end = start + len(ch.content)
                ch.meta = {**(ch.meta or {}), "in_link": bool(stack),
                           "refs": {off - start: t for off, t in targets.items() if start <= off < end}}

    def _classify_refs(self, text: str) -> dict:
        """Map each § ref offset to a local numbered heading id, or None to leave it as plain text."""
        refs = list(REF_RE.finditer(text))
        targets = {}
        for i, m in enumerate(refs):
            j = i
            while j > 0 and CHAIN_SEP.fullmatch(text[refs[j - 1].end():refs[j].start()]):
                j -= 1
            foreign = bool(FOREIGN_DOC.search(text[max(0, refs[j].start() - 60):refs[j].start()]))
            target = None if foreign or not self.opts.section_links else self.section_ids.get(m.group(1))
            self.report["refs_linked" if target else "refs_plain"] += 1
            targets[m.start()] = target
        return targets

    def resolve_anchor(self, frag: str) -> str | None:
        f = urllib.parse.unquote(frag)
        if f in self.anchors:
            return f
        return self.loose_map.get(loose(f)) if loose(f) else None

    def link(self, href: str) -> tuple[str | None, str]:
        """-> (href, css class). href None means the link cannot work in a PDF and becomes plain text."""
        h = href.strip()
        if re.match(r"(?i)^(https?|mailto|ftp):", h):
            return h, "ext"
        if re.match(r"(?i)^[a-z][a-z0-9+.\-]*:", h) and not re.match(r"^[A-Za-z]:[\\/]", h):
            return None, "plain"
        path, _, frag = h.partition("#")
        if path:
            with contextlib.suppress(OSError, ValueError):
                if (self.base / urllib.parse.unquote(path.split("?")[0])).resolve() == self.src and frag:
                    path = ""
            if path:
                return None, "docref"
        if not frag:
            return None, "plain"
        target = self.resolve_anchor(frag)
        if target is None:
            self.report["broken_anchors"].append("#" + frag)
            return None, "plain"
        return "#" + target, "xref"

    def toc_entries(self) -> list[dict]:
        top = self.chapter_level
        return [] if top is None else [h for h in self.headings if h["level"] in (top, top + 1) and not h["nested"]]

    def toc_html(self) -> str:
        rows = self.toc_entries()
        if not rows:
            return ""
        numbered, items = any(h["num"] for h in rows), []
        for h in rows:
            num = f'<span class="n">{esc(h["num"])}</span>' if numbered else ""
            items.append(f'<li class="t{h["level"] - self.chapter_level}"><a href="#{attr(h["id"])}">{num}'
                         f'<span class="t">{esc(h["label"] if numbered else h["text"])}</span><span class="dots"></span>'
                         f'<span class="pg" data-id="{attr(h["id"])}">000</span></a></li>')
        tag = f"h{self.chapter_level}"
        return (f'<nav class="toc{"" if numbered else " nonum"}"><{tag} class="r0" id="md2pdf-contents">Contents</{tag}>'
                f'<ol>{"".join(items)}</ol></nav>\n')

    def inline_toc(self) -> str:
        """[[_TOC_]] / [TOC]: a plain list of links, shown in compact layout (book layout has a contents page)."""
        rows = self.toc_entries()
        items = "".join(f'<li class="t{h["level"] - self.chapter_level}"><a class="xref" href="#{attr(h["id"])}">'
                        f'{esc(h["text"])}</a></li>' for h in rows)
        return f'<nav class="inline-toc"><ul>{items}</ul></nav>\n' if rows else ""


def render_text(self, tokens, idx, options, env):
    tok = tokens[idx]
    meta = tok.meta or {}
    lead = f'<span class="alert-title">{meta["alert"]}</span>' if meta.get("alert") else ""
    if meta.get("in_link"):
        return lead + esc(tok.content)
    refs, out, pos = meta.get("refs", {}), [lead], 0
    for m in INLINE_RE.finditer(tok.content):
        out.append(esc(tok.content[pos:m.start()]))
        found = m.group(0)
        if found.startswith("http"):
            url = found.rstrip(".,;:!?")
            out.append(f'<a class="url" href="{attr(url)}">{with_breaks(url, url=True)}</a>{esc(found[len(url):])}')
        elif refs.get(m.start()):
            out.append(f'<a class="xref" href="#{attr(refs[m.start()])}">{esc(found)}</a>')
        else:
            out.append(esc(found))
        pos = m.end()
    out.append(esc(tok.content[pos:]))
    return "".join(out)


def render_code_inline(self, tokens, idx, options, env):
    return f"<code>{with_breaks(tokens[idx].content)}</code>"


def render_html_block(self, tokens, idx, options, env):
    doc, content = env["doc"], tokens[idx].content
    return doc.inline_toc() if content.strip() == TOC_MARK else sanitize_html(content, doc)


def render_html_inline(self, tokens, idx, options, env):
    return sanitize_html(tokens[idx].content, env["doc"])


def render_image(self, tokens, idx, options, env):
    doc, tok = env["doc"], tokens[idx]
    src = str(tok.attrGet("src") or "")
    url, _ = resolve_src(src, doc.base)
    if url is None:
        doc.report["missing_images"].append(src)
        return f'<span class="img-missing">[image not found: {esc(src)}]</span>'
    alt = self.renderInlineAsText(tok.children or [], options, env)
    title = tok.attrGet("title")
    return f'<img src="{attr(url)}" alt="{attr(alt)}"' + (f' title="{attr(str(title))}"' if title else "") + ">"


def render_heading_open(self, tokens, idx, options, env):
    tok = tokens[idx]
    return (CHAPTER_MARK if (tok.meta or {}).get("chapter") else "") + self.renderToken(tokens, idx, options, env)


def code_line(line: str, lang: str) -> str:
    m = None
    if lang in ("json", "jsonc", "js", "javascript", "ts", "typescript", "tsx", "jsx", "cs", "csharp", "cpp", "c", "java",
                "go", "rust", "kotlin", "swift"):
        m = re.search(r"(?:^|(?<=\s))//.*$", line)
    elif lang in ("yaml", "yml", "powershell", "ps1", "pwsh", "sh", "bash", "python", "py", "toml", "ruby", "r"):
        m = re.search(r"(?:^|(?<=\s))#(?:\s.*)?$", line)
    if not m:
        return esc(line)
    return esc(line[:m.start()]) + '<span class="cm">' + esc(line[m.start():]) + "</span>"


def code_html(content: str, lang: str, layout: Layout) -> str:
    """Shrink each block just enough to fit its longest line; diagrams never wrap, code wraps as a last resort."""
    lines = [ln.expandtabs(4) for ln in content.rstrip("\n").split("\n")]
    longest = max((len(ln) for ln in lines), default=1) or 1
    fit = (layout.content_w_pt - PRE_CHROME_PT) / (longest * MONO_ADVANCE_EM)
    top = 8.5 * layout.scale
    floor = min(top, 6.8 if lang in ("", "text", "txt", "plain") else 7.3)
    size = int(min(top, max(floor, fit)) * 100) / 100
    mode = "wrap" if fit < floor else "nowrap"
    body = "".join(f'<span class="ln">{code_line(ln, lang) or " "}</span>' for ln in lines)
    return f'<pre class="code {mode}" style="font-size:{size}pt"><code>{body}</code></pre>\n'


def render_fence(self, tokens, idx, options, env):
    doc, tok = env["doc"], tokens[idx]
    info = tok.info.strip() if tok.type == "fence" else ""
    lang = info.split()[0].lower() if info else ""
    if lang == "mermaid" and doc.opts.mermaid:
        return (f'<figure class="mermaid-block"><div class="mermaid-src">{esc(tok.content)}</div>'
                f'<div class="mermaid-fallback">{code_html(tok.content, "", doc.layout)}'
                f'<p class="note">Mermaid diagram shown as source: <span class="why"></span></p></div></figure>\n')
    return code_html(tok.content, lang, doc.layout)


RENDER_RULES = {"text": render_text, "code_inline": render_code_inline, "html_block": render_html_block,
                "html_inline": render_html_inline, "image": render_image, "heading_open": render_heading_open,
                "fence": render_fence, "code_block": render_fence}


def build_html(doc: Doc, mode: str, breaks: str, mermaid_js: pathlib.Path | None) -> str:
    render = lambda ts: doc.md.renderer.render(ts, doc.md.options, doc.env)
    lay = doc.layout
    parts = render(doc.body).split(CHAPTER_MARK)
    sections = [f'<section class="preamble">{parts[0]}</section>\n'] if parts[0].strip() else []
    for p in parts[1:]:
        m = re.match(r'\s*<h\d id="([^"]+)"', p)
        sections.append(f'<section class="chapter" data-id="{m.group(1) if m else ""}">{p}</section>\n')
    title_html = render([doc.tokens[doc.title_head["index"] + 1]]) if doc.title_head else esc(doc.title)
    lede = render(doc.lede)
    if not lede and doc.meta.get("description"):
        lede = f'<p class="lede">{esc(doc.meta["description"])}</p>'
    mtime = dt.datetime.fromtimestamp(doc.src.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    bits = [esc(doc.meta[k]) for k in ("author", "date") if doc.meta.get(k)]
    bits += [f"Source: {esc(doc.src.name)} (modified {mtime})",
             f"Laid out for {esc(lay.label)}, {lay.w:g} &times; {lay.h:g} mm", f"Rendered {dt.date.today().isoformat()}"]
    title_id = doc.title_head["id"] if doc.title_head else "md2pdf-title"
    header = (f'<header class="titleblock"><div class="cover-main"><div class="doc-title" id="{attr(title_id)}">{title_html}'
              f'</div>{lede}</div><div class="meta">{" &middot; ".join(bits)}</div></header>\n')
    footer = footer_title(doc).replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\3C ").replace("\n", " ")
    css = CSS
    for key, val in {"W": lay.w, "H": lay.h, "MT": lay.mt, "MR": lay.mr, "MB": lay.mb, "ML": lay.ml,
                     "FONT": lay.font, "FOOT": round(7.5 * min(1.0, lay.scale), 2), "PGNUM": round(8 * min(1.0, lay.scale), 2),
                     "COVER": round(lay.content_h_mm - 4, 2), "IMGMAX": round(lay.content_h_mm * 0.9, 2),
                     "DIAGMAX": round(lay.content_h_mm * 0.85, 2)}.items():
        css = css.replace(f"__{key}__", f"{val:g}")
    css = css.replace("__FOOTER__", footer)
    script = f'<script src="{attr(mermaid_js.as_uri())}"></script>\n' if doc.mermaid_count and mermaid_js else ""
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>{esc(doc.title)}</title>'
            f'<style>{css}</style></head><body class="layout-{mode} breaks-{breaks}">\n{header}{doc.toc_html()}'
            f'<main>\n{"".join(sections)}</main>\n{script}</body></html>\n')


CSS = """
@page {
  size: __W__mm __H__mm;
  margin: __MT__mm __MR__mm __MB__mm __ML__mm;
  @bottom-left { content: "__FOOTER__"; font-family: "Segoe UI", sans-serif; font-size: __FOOT__pt; color: #333; }
  @bottom-right { content: counter(page) " / " counter(pages); font-family: "Segoe UI", sans-serif; font-size: __PGNUM__pt; color: #111; }
}
@page cover { @bottom-left { content: none; } @bottom-right { content: none; } }
:root { --accent: #1c3d66; }
html { font-size: __FONT__pt; }
body { margin: 0; color: #000; font-family: Cambria, "Sitka Text", Georgia, "Noto Serif", serif; line-height: 1.4;
  font-kerning: normal; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
p, li { text-wrap: pretty; }
p { margin: 0 0 0.59rem; orphans: 3; widows: 3; }
ul, ol { margin: 0 0 0.64rem; padding-left: 1.36rem; }
li { margin: 0 0 0.23rem; }
li > ul, li > ol { margin: 0.23rem 0 0; }
li > p { margin: 0 0 0.27rem; }
li.task-list-item { list-style: none; }
li.task-list-item input.task-list-item-checkbox:first-child { margin-left: -1.25em; }
input.task-list-item-checkbox { width: 0.85em; height: 0.85em; margin: 0 0.4em 0 0; vertical-align: -0.08em; }
p:has(+ table), p:has(+ pre), p:has(+ ul), p:has(+ ol), p:has(+ figure) { break-after: avoid; }
h1, h2, h3, h4, h5, h6, .doc-title { font-family: "Segoe UI", system-ui, sans-serif; font-weight: 600; color: var(--accent);
  line-height: 1.22; text-wrap: balance; break-after: avoid; }
h1, h2, h3, h4, h5, h6 { font-size: 1rem; margin: 0.9rem 0 0.36rem; }
.r0 { font-size: 1.5rem; margin: 1.6rem 0 0.9rem; padding-bottom: 0.36rem; border-bottom: 1.5pt solid var(--accent); }
body.breaks section.chapter > .r0:first-child, nav.toc > .r0, main > section:first-child > .r0:first-child { margin-top: 0; }
.r1 { font-size: 1.14rem; margin: 1.27rem 0 0.45rem; }
.r2 { font-size: 1rem; margin: 0.9rem 0 0.36rem; }
.r3 { font-size: 0.95rem; margin: 0.8rem 0 0.3rem; color: #000; }
strong, b { font-weight: 700; }
a { color: inherit; text-decoration: none; }
a.xref, a.ext, a.url { color: var(--accent); text-decoration: underline; text-decoration-color: #8ea5c4;
  text-decoration-thickness: 0.6pt; text-underline-offset: 1.6pt; }
a.url { overflow-wrap: anywhere; }
span.docref { text-decoration: underline dotted #888; text-decoration-thickness: 0.6pt; text-underline-offset: 1.6pt; }
blockquote { margin: 0.73rem 0 0.9rem; padding: 0.55rem 0.9rem; border-left: 3pt solid var(--accent); background: #eef1f5; }
blockquote > :last-child { margin-bottom: 0; }
blockquote.alert { background: #f1f1f1; border-left-color: #333; }
.alert-title { display: block; font-family: "Segoe UI", sans-serif; font-weight: 700; font-size: 0.9rem; margin-bottom: 0.15rem; }
hr { border: 0; border-top: 0.75pt solid #999; margin: 0.9rem 0; }
kbd { font-family: Consolas, "Cascadia Mono", monospace; font-size: 0.85em; border: 0.75pt solid #888; border-radius: 2px; padding: 0 0.2em; }
mark { background: #ddd; color: inherit; }
del, s, strike { color: #444; }
details { margin: 0.4rem 0 0.8rem; padding-left: 0.6rem; border-left: 1.5pt solid #bbb; }
summary { font-weight: 600; margin-bottom: 0.3rem; list-style: none; }
summary::-webkit-details-marker { display: none; }
img { max-width: 100%; height: auto; max-height: __IMGMAX__mm; }
p > img:only-child, p > a:only-child > img:only-child { display: block; margin: 0.3rem auto; }
.img-missing { display: inline-block; border: 0.75pt dashed #777; padding: 0.1rem 0.3rem; font-size: 0.8rem; color: #444; }
figure { margin: 0.5rem 0 0.9rem; }
figure.mermaid-block { text-align: center; break-inside: avoid; }
figure.mermaid-block svg { display: block; margin: 0 auto; max-width: 100%; height: auto; max-height: __DIAGMAX__mm; }
.mermaid-fallback { display: none; text-align: left; }
figure.failed .mermaid-src { display: none; }
figure.failed .mermaid-fallback { display: block; }
.mermaid-fallback .note { font-size: 0.8rem; font-style: italic; color: #444; }
code { font-family: Consolas, "Cascadia Mono", Menlo, monospace; font-size: 0.87em; background: #ebebeb;
  border-radius: 2px; padding: 0 0.14rem; overflow-wrap: break-word; }
th code, td code { overflow-wrap: anywhere; }
pre.code { margin: 0.45rem 0 0.9rem; padding: 5pt 6pt; background: #f3f3f3; border: 0.75pt solid #9a9a9a;
  border-left: 2.5pt solid #4d4d4d; line-height: 1.3; font-family: Consolas, "Cascadia Mono", Menlo, monospace; }
pre.code.keep { break-inside: avoid; }
pre.code code { font-size: inherit; background: none; padding: 0; border-radius: 0; }
pre.code .ln { display: block; break-inside: avoid; }
pre.code.nowrap .ln { white-space: pre; }
pre.code.wrap .ln { white-space: pre-wrap; overflow-wrap: anywhere; padding-left: 2ch; text-indent: -2ch; }
pre.code .cm { font-style: italic; color: #3b3b3b; }
table { width: 100%; border-collapse: collapse; margin: 0.45rem 0 1rem; font-family: Calibri, "Segoe UI", sans-serif;
  font-size: 0.873rem; line-height: 1.27; }
table.cols-5, table.cols-6 { font-size: 0.818rem; }
table.cols-7, table.cols-8 { font-size: 0.745rem; }
table.cols-many { font-size: 0.682rem; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
table.keep, .nobreak { break-inside: avoid; }
th, td { border: 0.75pt solid #777; padding: 0.25rem 0.41rem; vertical-align: top; text-align: left; }
th { background: #e1e6ed; font-weight: 700; }
table.squeeze th, table.squeeze td { padding: 0.15rem 0.25rem; overflow-wrap: anywhere; }
td ul, td ol { margin: 0; padding-left: 1rem; }
td li { margin: 0 0 0.09rem; }
td > p:last-child, th > p:last-child { margin-bottom: 0; }
.footnotes { font-size: 0.85rem; }
.footnote-ref a, a.footnote-backref { color: var(--accent); }
header.titleblock .doc-title { font-size: 2.36rem; line-height: 1.15; margin: 0 0 1.6rem; }
header.titleblock p.lede { font-size: 1.05rem; }
header.titleblock blockquote { font-size: 0.955rem; }
header.titleblock .meta { font-family: "Segoe UI", sans-serif; font-size: 0.73rem; color: #333; border-top: 0.75pt solid #999; padding-top: 0.45rem; }
body.book header.titleblock { page: cover; min-height: __COVER__mm; display: flex; flex-direction: column; break-after: page; }
body.book .cover-main { flex: 1; display: flex; flex-direction: column; justify-content: center; }
body.compact header.titleblock { margin-bottom: 1.2rem; }
body.compact header.titleblock .doc-title { font-size: 1.8rem; margin-bottom: 0.7rem; }
body.compact nav.toc, body.book nav.inline-toc { display: none; }
body.breaks section.chapter { break-before: page; }
body.breaks section.preamble.short + section.chapter, body.breaks main > section.chapter:first-child { break-before: auto; }
nav.toc { break-after: page; }
nav.toc ol { list-style: none; margin: 0; padding: 0; }
nav.toc li { margin: 0; }
nav.toc li.t0 { margin-top: 0.64rem; font-family: "Segoe UI", sans-serif; font-weight: 600; font-size: 0.94rem; break-after: avoid; }
nav.toc li.t1 { font-size: 0.89rem; padding-left: 1.9em; }
nav.toc.nonum li.t1 { padding-left: 1.2em; }
nav.toc a { display: flex; align-items: baseline; color: #000; padding: 1pt 0; }
nav.toc .n { flex: none; width: 1.9em; }
nav.toc li.t1 .n { width: 2.5em; }
nav.toc .t { flex: 0 1 auto; }
nav.toc .dots { flex: 1 1 auto; min-width: 10pt; margin: 0 4pt; border-bottom: 0.9pt dotted #666; }
nav.toc .pg { flex: none; width: 22pt; text-align: right; font-family: "Segoe UI", sans-serif; font-variant-numeric: tabular-nums; }
nav.toc li.t1 .pg { font-weight: 400; }
nav.inline-toc { margin: 0.4rem 0 1rem; padding: 0.45rem 0.8rem; border: 0.75pt solid #aaa; font-size: 0.9rem; break-inside: avoid; }
nav.inline-toc ul { list-style: none; margin: 0; padding: 0; }
nav.inline-toc li { margin: 0 0 0.1rem; }
nav.inline-toc li.t1 { padding-left: 1.2rem; }
section.tight { line-height: 1.34; }
section.tight p { margin-bottom: 0.45rem; }
section.tight li { margin-bottom: 0.14rem; }
section.tight .r1 { margin-top: 1rem; }
section.tight table { line-height: 1.2; margin-bottom: 0.82rem; }
section.tight th, section.tight td { padding-top: 0.17rem; padding-bottom: 0.17rem; }
section.tight pre.code { line-height: 1.24; }
"""


# The page scripts run in Edge with print media at the page's content width, before printing.
# Chromium splits a short paragraph against its widows setting rather than moving it, so short blocks are made
# unbreakable. Tables and code blocks are kept whole only when moving one to the next page leaves a small gap.
PREPARE_JS = r"""((P) => {
  const main = document.querySelector('main'), body = document.body;
  const px = (el, prop) => parseFloat(getComputedStyle(el)[prop]) || 0;
  const height = el => el ? el.getBoundingClientRect().height : 0;
  const stats = {shrunkCode: 0, wrappedCode: 0, zoomedTables: 0, squeezedTables: 0, keptTables: 0, keptCode: 0, nobreak: 0};
  for (const pre of document.querySelectorAll('pre.code.nowrap')) {
    if (pre.scrollWidth <= pre.clientWidth + 1) continue;
    const pad = px(pre, 'paddingLeft') + px(pre, 'paddingRight');
    const size = px(pre, 'fontSize') * (pre.clientWidth - pad) / (pre.scrollWidth - pad) * 0.995;
    if (size >= P.codeFloor) { pre.style.fontSize = size.toFixed(2) + 'px'; stats.shrunkCode++; }
    if (pre.scrollWidth > pre.clientWidth + 1) { pre.classList.replace('nowrap', 'wrap'); stats.wrappedCode++; }
  }
  for (const t of document.querySelectorAll('table')) {
    const box = t.parentElement, avail = box.clientWidth - px(box, 'paddingLeft') - px(box, 'paddingRight');
    let w = t.getBoundingClientRect().width;
    if (w <= avail + 0.5) continue;
    if (avail / w < 0.8) {  // too wide to just shrink: let cell text break anywhere, then shrink what is left
      t.classList.add('squeeze'); stats.squeezedTables++;
      w = t.getBoundingClientRect().width;
    }
    if (w > avail + 0.5) { t.style.zoom = Math.max(0.5, avail / w * 0.99).toFixed(3); stats.zoomedTables++; }
  }
  for (const t of main.querySelectorAll('table')) {
    const keep = height(t) <= P.keepTable;
    t.classList.toggle('keep', keep);
    if (keep) stats.keptTables++;
  }
  for (const pre of main.querySelectorAll('pre.code')) {
    if (height(pre) <= P.keepCode) { pre.classList.add('keep'); stats.keptCode++; }
  }
  for (const el of main.querySelectorAll('p, li, blockquote')) {
    if (el.closest('td, th')) continue;
    const lh = px(el, 'lineHeight') || 20;
    if (height(el) / lh <= P.lines + 0.5) { el.classList.add('nobreak'); stats.nobreak++; }
  }
  const est = height(main) / P.pageH;
  const chapters = [...main.querySelectorAll(':scope > section.chapter')];
  const pre = main.querySelector(':scope > section.preamble');
  const short = !!pre && height(pre) < 0.5 * P.pageH;
  if (pre) pre.classList.toggle('short', short);
  let mode = P.mode;
  if (mode === 'auto') mode = est >= P.bookPages && P.tocEntries >= 4 ? 'book' : 'compact';
  let breaks = P.breaks === 'on';
  if (P.breaks === 'auto' && mode === 'book' && chapters.length >= 2) {
    const paged = chapters.reduce((n, c) => n + Math.ceil(height(c) / P.pageH), 0)
      + (pre && !short ? Math.ceil(height(pre) / P.pageH) : 0);
    breaks = paged <= Math.ceil(est) * P.breakRatio;
  }
  body.classList.add(mode, breaks ? 'breaks' : 'flow');
  return {mode, breaks, estPages: Math.round(est * 10) / 10, chapters: chapters.length, stats};
})"""

# Elements that stick out of the page (they would be clipped in the PDF), and images that failed to load.
OVERFLOW_JS = r"""(() => {
  const W = document.documentElement.clientWidth, bad = new Set(), items = [];
  const describe = el => {
    const cls = typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/).join('.') : '';
    return `${el.tagName.toLowerCase()}${cls} "${(el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 50)}"`;
  };
  for (const el of document.querySelectorAll('header *, nav *, main *')) {
    if (el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const over = r.right > W + 0.5 || r.left < -0.5 || (el.tagName === 'PRE' && el.scrollWidth > el.clientWidth + 1);
    if (!over) continue;
    bad.add(el);
    if (!bad.has(el.parentElement)) items.push(describe(el));
  }
  const brokenImages = [...document.images].filter(i => i.complete && i.naturalWidth === 0).map(i => i.getAttribute('src'));
  return {width: W, overflow: items.length, items: items.slice(0, 10), brokenImages};
})()"""

# Renders every diagram locally with the cached mermaid.js. A diagram that fails shows its source instead.
MERMAID_JS = r"""(async (why) => {
  const figs = [...document.querySelectorAll('figure.mermaid-block')];
  const fail = (f, msg) => { f.classList.add('failed'); const w = f.querySelector('.why'); if (w) w.textContent = msg; };
  if (typeof mermaid === 'undefined') {
    figs.forEach(f => fail(f, why));
    return {count: figs.length, failed: figs.length, errors: [why]};
  }
  mermaid.initialize({startOnLoad: false, theme: 'neutral', securityLevel: 'strict', deterministicIds: true,
                      suppressErrorRendering: true, fontFamily: '"Segoe UI", sans-serif'});
  const errors = [];
  for (const f of figs) {
    // Not class="mermaid": mermaid would draw those itself on load, with its default settings and error pictures.
    const node = f.querySelector('.mermaid-src');
    try {
      await mermaid.run({nodes: [node], suppressErrors: false});
      if (!node.querySelector('svg')) throw new Error('mermaid produced no drawing');
    } catch (e) {
      const msg = String((e && (e.message || e.str)) || e).split('\n')[0].slice(0, 200);
      errors.push(msg);
      fail(f, msg);
    }
  }
  return {count: figs.length, failed: errors.length, errors};
})"""

# Book layout: fills in the contents page numbers found in the previous print and tightens runt chapters.
APPLY_JS = r"""((pages, tight) => {
  for (const s of document.querySelectorAll('nav.toc .pg')) s.textContent = pages[s.dataset.id] ?? '';
  for (const s of document.querySelectorAll('main > section.chapter')) s.classList.toggle('tight', tight.includes(s.dataset.id));
  return true;
})"""

SNAPSHOT_JS = r"""(() => {
  const root = document.documentElement.cloneNode(true);
  root.querySelectorAll('script').forEach(s => s.remove());
  return '<!doctype html>\n' + root.outerHTML;
})()"""


MAX_PASSES = 6
RUNT_CHARS = 400  # a chapter whose last Paper Pro page has less text than this gets tighter spacing
# Headless Edge with nothing that phones home: no sync, extensions, component updates, crash reports or pings.
EDGE_FLAGS = ["--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
              "--disable-sync", "--hide-scrollbars", "--mute-audio", "--disable-background-networking",
              "--disable-component-update", "--disable-default-apps", "--disable-domain-reliability",
              "--disable-client-side-phishing-detection", "--disable-breakpad", "--no-pings",
              "--remote-debugging-port=0"]
EDGE_POLICIES = {"RemoteDebuggingAllowed": (0, "remote debugging is turned off"),
                 "DeveloperToolsAvailability": (2, "developer tools are turned off"),
                 "HeadlessModeEnabled": (0, "headless mode is turned off")}


class Cdp:
    """Minimal Chrome DevTools Protocol client over Edge's browser-level WebSocket (flattened sessions)."""

    def __init__(self, ws):
        self.ws, self.seq, self.pending, self.waiters = ws, 0, {}, []

    async def pump(self) -> None:
        try:
            with contextlib.suppress(ConnectionClosed):
                async for raw in self.ws:
                    msg = json.loads(raw)
                    if "id" in msg:
                        fut = self.pending.pop(msg["id"], None)
                        if fut is not None and not fut.done():
                            fut.set_result(msg)
                        continue
                    for w in list(self.waiters):
                        method, session, fut = w
                        if fut.done():
                            self.waiters.remove(w)
                        elif msg.get("method") == method and msg.get("sessionId") == session:
                            fut.set_result(msg.get("params", {}))
                            self.waiters.remove(w)
        finally:
            lost = ConnectionError("lost the connection to Edge (did it crash?)")
            for fut in [*self.pending.values(), *(w[2] for w in self.waiters)]:
                if not fut.done():
                    fut.set_exception(lost)
            self.pending.clear()
            self.waiters.clear()

    async def send(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 120):
        self.seq += 1
        key = self.seq
        fut = asyncio.get_running_loop().create_future()
        self.pending[key] = fut
        msg = {"id": key, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            await self.ws.send(json.dumps(msg))
            res = await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise TimeoutError(f"Edge did not answer {method} within {timeout:g} s") from None
        except ConnectionClosed:
            raise ConnectionError("lost the connection to Edge (did it crash?)") from None
        finally:
            self.pending.pop(key, None)
        if "error" in res:
            err = res["error"]
            raise RuntimeError(f"{method}: {err.get('message', err) if isinstance(err, dict) else err}")
        return res.get("result", {})

    def expect(self, method: str, session: str | None = None) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())  # an unawaited failure is not an error
        self.waiters.append((method, session, fut))
        return fut


def find_edge(explicit: str | None) -> str:
    """Microsoft Edge only, by design: documents are never handed to another browser."""
    given = explicit or os.environ.get("MD2PDF_EDGE")
    if given:
        p = pathlib.Path(given).expanduser()
        if p.is_dir():
            p = p / ("msedge.exe" if os.name == "nt" else "microsoft-edge")
        if not p.is_file():
            raise SetupError(f"Edge was not found at {p}")
        if "edge" not in p.name.lower():
            raise SetupError(f"{p.name} is not Microsoft Edge; md2pdf only renders with Edge (msedge)")
        return str(p)
    cands: list[pathlib.Path] = []
    if os.name == "nt":
        for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            if os.environ.get(var):
                cands += [pathlib.Path(os.environ[var]) / "Microsoft" / ch / "Application" / "msedge.exe"
                          for ch in ("Edge", "Edge Beta", "Edge Dev", "Edge SxS")]
    elif sys.platform == "darwin":
        cands += [pathlib.Path(f"/Applications/Microsoft Edge{ch}.app/Contents/MacOS/Microsoft Edge{ch}")
                  for ch in ("", " Beta", " Dev", " Canary")]
    for p in cands:
        if p.is_file():
            return str(p)
    for name in ("msedge", "microsoft-edge", "microsoft-edge-stable", "microsoft-edge-beta", "microsoft-edge-dev"):
        found = shutil.which(name)
        if found:
            return found
    raise SetupError("Microsoft Edge was not found. Install it from https://www.microsoft.com/edge, "
                     "or point --browser (or MD2PDF_EDGE) at msedge.")


def edge_policies() -> dict[str, str]:
    """Edge group policies that stop md2pdf from driving Edge: {policy name: explanation}."""
    found: dict[str, str] = {}
    if os.name != "nt":
        return found
    import winreg
    for hive, hname in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Policies\Microsoft\Edge")
        except OSError:
            continue
        with key:
            for name, (bad, what) in EDGE_POLICIES.items():
                with contextlib.suppress(OSError):
                    if winreg.QueryValueEx(key, name)[0] == bad:
                        found.setdefault(name, f"{what} by policy ({hname}\\SOFTWARE\\Policies\\Microsoft\\Edge\\{name} = {bad})")
    return found


def launch_edge(exe: str, work: pathlib.Path):
    blocked = edge_policies()
    if "RemoteDebuggingAllowed" in blocked:
        raise SetupError(f"md2pdf cannot drive Edge on this PC: {blocked['RemoteDebuggingAllowed']}")
    profile = work / "edge-profile"
    shutil.rmtree(profile, ignore_errors=True)
    # An app-compatibility layer inherited from the host (__COMPAT_LAYER, set by some terminals) makes Edge exit at once.
    env = {k: v for k, v in os.environ.items() if k.upper() != "__COMPAT_LAYER"}
    proc = subprocess.Popen([exe, *EDGE_FLAGS, f"--user-data-dir={profile}", "about:blank"], env=env,  # noqa: S603 - Edge only
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file, deadline = profile / "DevToolsActivePort", time.time() + 30
    while time.time() < deadline:
        with contextlib.suppress(OSError):
            parts = port_file.read_text(encoding="utf-8", errors="ignore").split()
            if len(parts) >= 2:
                return proc, f"ws://127.0.0.1:{parts[0]}{parts[1]}", profile
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    code = proc.poll()
    with contextlib.suppress(OSError):
        proc.kill()
    why = "; ".join(blocked.values()) or (f"it exited with code {code}" if code is not None
                                          else "it did not open its DevTools port within 30 s")
    raise SetupError(f"could not start headless Edge ({exe}): {why}")


class Browser:
    """One headless Edge for the whole run; each document gets its own tab."""

    def __init__(self, exe: str, work: pathlib.Path):
        self.exe, self.work = exe, work
        self.proc = self.ws = self.cdp = self.pump = self.profile = None

    async def start(self) -> Browser:
        self.proc, url, self.profile = launch_edge(self.exe, self.work)
        try:
            self.ws = await ws_connect(url, max_size=None, proxy=None, open_timeout=20, ping_interval=None)
        except Exception as e:  # noqa: BLE001 - any failure here means Edge is unusable
            raise SetupError(f"could not connect to headless Edge: {e}") from e
        self.cdp = Cdp(self.ws)
        self.pump = asyncio.create_task(self.cdp.pump())
        return self

    def alive(self) -> bool:
        return self.pump is not None and not self.pump.done() and self.proc.poll() is None

    async def new_tab(self) -> tuple[str, str]:
        target = (await self.cdp.send("Target.createTarget", {"url": "about:blank"}))["targetId"]
        sid = (await self.cdp.send("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
        await self.cdp.send("Page.enable", session=sid)
        return target, sid

    async def close_tab(self, target: str) -> None:
        with contextlib.suppress(Exception):
            await self.cdp.send("Target.closeTarget", {"targetId": target}, timeout=10)

    async def close(self) -> None:
        if self.cdp is not None and self.alive():
            with contextlib.suppress(Exception):
                await self.cdp.send("Browser.close", timeout=5)
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
        if self.pump is not None:
            self.pump.cancel()
        if self.proc is not None:
            try:
                self.proc.wait(15 if self.cdp is not None else 1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                with contextlib.suppress(Exception):
                    self.proc.wait(5)
        if self.profile is not None:
            for _ in range(10):  # Edge's helper processes can hold profile files for a moment after it exits
                shutil.rmtree(self.profile, ignore_errors=True)
                if not self.profile.exists():
                    break
                await asyncio.sleep(0.3)


def resolved(d, key, default=None):
    v = d.get(key) if d is not None else None
    return default if v is None else v.get_object()


def dest_pages(reader, entries: list[dict]) -> dict:
    """Page number of each heading: from the PDF's named destinations, else from the bookmark titles."""
    pages: dict = {}
    with contextlib.suppress(Exception):
        for name, dest in reader.named_destinations.items():
            with contextlib.suppress(Exception):
                pages[str(name).lstrip("/")] = reader.get_destination_page_number(dest) + 1
    if any(h["id"] not in pages for h in entries):
        by_title: dict = {}

        def walk(items):
            for it in items:
                if isinstance(it, list):
                    walk(it)
                else:
                    with contextlib.suppress(Exception):
                        by_title.setdefault(" ".join(str(it.title).split()), reader.get_destination_page_number(it) + 1)
        with contextlib.suppress(Exception):
            walk(reader.outline)
        for h in entries:
            pages.setdefault(h["id"], by_title.get(" ".join(h["text"].split())))
    return {h["id"]: pages.get(h["id"]) for h in entries}


def runt_pages(reader, chapter_ids: list[str], found: dict, footer: str, threshold: float) -> list[tuple]:
    """Chapters whose last page holds only a few lines: (chapter id, page, characters on that page)."""
    n = len(reader.pages)
    starts = [found.get(i) for i in chapter_ids]
    if len(chapter_ids) < 2 or None in starts:
        return []
    runts = []
    for k, cid in enumerate(chapter_ids):
        last = starts[k + 1] - 1 if k + 1 < len(chapter_ids) else n
        if last <= starts[k]:
            continue
        chrome = len(re.sub(r"\s", "", f"{footer}{last}/{n}"))
        chars = len(re.sub(r"\s", "", reader.pages[last - 1].extract_text() or "")) - chrome
        if chars < threshold:
            runts.append((cid, last, chars))
    return runts


def pdf_summary(reader) -> dict:
    count = lambda items: sum(count(i) if isinstance(i, list) else 1 for i in items)
    internal = external = 0
    for p in reader.pages:
        for a in resolved(p, "/Annots", []):
            a = a.get_object()
            if a.get("/Subtype") != "/Link":
                continue
            act = resolved(a, "/A", {})
            if "/Dest" in a or act.get("/S") == "/GoTo":
                internal += 1
            elif act.get("/S") == "/URI":
                external += 1
    bookmarks = 0
    with contextlib.suppress(Exception):
        bookmarks = count(reader.outline)
    return {"pages": len(reader.pages), "bookmarks": bookmarks, "internal_links": internal, "external_links": external}


class UsageError(Exception):
    """Bad command line: missing inputs or conflicting options."""


def footer_title(doc: Doc) -> str:
    limit = max(24, int(doc.layout.content_w_mm * 0.55))
    return doc.title if len(doc.title) <= limit else doc.title[:limit - 1].rstrip() + "\u2026"


async def evaluate(cdp: Cdp, sid: str, expr: str, await_promise: bool = False, timeout: float = 120):
    res = await cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": await_promise},
                         session=sid, timeout=timeout)
    if "exceptionDetails" in res:
        d = res["exceptionDetails"]
        raise RuntimeError(f"page script failed: {(d.get('exception') or {}).get('description') or d.get('text')}")
    return (res.get("result") or {}).get("value")


async def print_pdf(cdp: Cdp, sid: str, layout: Layout) -> bytes:
    res = await cdp.send("Page.printToPDF", layout.print_params(), session=sid, timeout=300)
    return base64.b64decode(res["data"])


class Mermaid:
    """mermaid.js is fetched at most once per run, and only when a document has a diagram."""

    def __init__(self, home: pathlib.Path):
        self.home, self.checked, self.path, self.why = home, False, None, ""

    def get(self) -> pathlib.Path | None:
        if not self.checked:
            self.checked = True
            self.path, how = ensure_mermaid(self.home)
            if self.path is None:
                self.why = f"mermaid.js could not be downloaded ({how})"
        return self.path


def write_file(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except PermissionError as e:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise RuntimeError(f"cannot write {path} (is it open in another program?)") from e


def as_path(url: str) -> str:
    return urllib.request.url2pathname(urllib.parse.urlparse(url).path) if url.lower().startswith("file:") else url


async def convert_one(browser: Browser, src: pathlib.Path, out: pathlib.Path, layout: Layout,
                      opts: argparse.Namespace, work: pathlib.Path, n: int, mermaid: Mermaid) -> dict:
    doc = Doc(src, layout, opts)
    warnings: list[str] = []
    mermaid_js = mermaid.get() if doc.mermaid_count else None
    page = work / f"doc{n}.html"
    page.write_text(build_html(doc, opts.layout, opts.breaks, mermaid_js), encoding="utf-8")
    cdp, entries = browser.cdp, doc.toc_entries()
    target, sid = await browser.new_tab()
    try:
        await cdp.send("Emulation.setEmulatedMedia", {"media": "print"}, session=sid)
        await cdp.send("Emulation.setDeviceMetricsOverride", {"width": round(layout.content_w_px), "height": 900,
                                                              "deviceScaleFactor": 1, "mobile": False}, session=sid)
        loaded = cdp.expect("Page.loadEventFired", sid)
        nav = await cdp.send("Page.navigate", {"url": page.as_uri()}, session=sid)
        if nav.get("errorText"):
            raise RuntimeError(f"Edge could not open the page: {nav['errorText']}")
        try:
            await asyncio.wait_for(loaded, 90)
        except TimeoutError:
            warnings.append("the page took over 90 s to load (slow remote images?); printed what had loaded")
        await evaluate(cdp, sid, "document.fonts.ready.then(() => true)", await_promise=True, timeout=30)
        diagrams = None
        if doc.mermaid_count:
            why = mermaid.why or "mermaid.js did not load"
            diagrams = await evaluate(cdp, sid, f"{MERMAID_JS}({json.dumps(why)})", await_promise=True, timeout=180)
        params = {"pageH": layout.content_h_px, "keepTable": 0.35 * layout.content_h_px,
                  "keepCode": 0.6 * layout.content_h_px, "lines": 6, "codeFloor": 6.8 * 96 / 72,
                  "mode": opts.layout, "breaks": opts.breaks, "bookPages": 6, "tocEntries": len(entries),
                  "breakRatio": 1.35}
        prep = await evaluate(cdp, sid, f"{PREPARE_JS}({json.dumps(params)})")
        checks = await evaluate(cdp, sid, OVERFLOW_JS) or {}
        await cdp.send("Emulation.clearDeviceMetricsOverride", session=sid)
        await cdp.send("Emulation.setEmulatedMedia", {"media": ""}, session=sid)
        pdf = await print_pdf(cdp, sid, layout)
        passes, tight, settled, found = 1, set(), True, {}
        if prep["mode"] == "book" and entries:
            # Contents page numbers come from the previous print. A chapter that ends in a near-empty page gets
            # tighter spacing, which moves the chapters after it, so printing repeats until nothing changes.
            chapter_ids = [h["id"] for h in entries if h["level"] == doc.chapter_level]
            area = layout.content_w_mm * layout.content_h_mm / (158.0 * 212.0)
            threshold, footer, applied = RUNT_CHARS * area / layout.scale ** 2, footer_title(doc), None
            while True:
                reader = PdfReader(io.BytesIO(pdf))
                found = dest_pages(reader, entries)
                runts = runt_pages(reader, chapter_ids, found, footer, threshold) if prep["breaks"] else []
                fresh = {cid for cid, _, _ in runts} - tight
                if found == applied and not fresh:
                    break
                if passes >= MAX_PASSES:
                    settled = False
                    break
                tight |= fresh
                applied = found
                await evaluate(cdp, sid, f"{APPLY_JS}({json.dumps(found)}, {json.dumps(sorted(tight))})")
                pdf = await print_pdf(cdp, sid, layout)
                passes += 1
        snapshot = await evaluate(cdp, sid, SNAPSHOT_JS) if opts.html else None
    finally:
        await browser.close_tab(target)

    summary = pdf_summary(PdfReader(io.BytesIO(pdf)))
    write_file(out, pdf)
    html_out = out.with_suffix(".html") if snapshot else None
    if html_out:
        write_file(html_out, snapshot.encode("utf-8"))
    rep = doc.report
    warnings += [f"image not found: {s}" for s in dict.fromkeys(rep["missing_images"])]
    warnings += [f"image did not load: {as_path(s)}" for s in dict.fromkeys(checks.get("brokenImages") or [])]
    warnings += [f"link to a heading that does not exist: {a}" for a in dict.fromkeys(rep["broken_anchors"])]
    if diagrams and diagrams.get("failed"):
        if mermaid_js is None:
            warnings.append(f"{diagrams['failed']} mermaid diagram(s) shown as source: {mermaid.why}")
        else:
            warnings += [f"mermaid diagram shown as source: {e}" for e in diagrams.get("errors") or []]
    if checks.get("overflow"):
        warnings.append(f"{checks['overflow']} element(s) wider than the page, e.g. {checks['items'][0]}")
    if not settled:
        warnings.append(f"contents page numbers may be off: the layout did not settle in {MAX_PASSES} prints")
    missing = [i for i, p in found.items() if not p]
    if missing:
        warnings.append(f"{len(missing)} contents entries have no page number")
    return {"input": str(src), "output": str(out), "ok": True, "title": doc.title, "pages": summary["pages"],
            "layout": prep["mode"], "chapter_breaks": bool(prep["breaks"]), "passes": passes,
            "device": layout.device, "page_mm": [round(layout.w, 1), round(layout.h, 1)], "font_pt": layout.font,
            "warnings": warnings, "section_refs": {"linked": rep["refs_linked"], "plain": rep["refs_plain"]},
            "mermaid": diagrams, "tightened_chapters": sorted(tight), "adjustments": prep.get("stats"),
            "bookmarks": summary["bookmarks"], "internal_links": summary["internal_links"],
            "external_links": summary["external_links"], "html": str(html_out) if html_out else None}


def show(path: str) -> str:
    """A path as the user would type it: relative when it is inside the current folder."""
    with contextlib.suppress(ValueError):
        rel = os.path.relpath(path)
        if not rel.startswith(".."):
            return rel
    return path


def print_result(res: dict, opts: argparse.Namespace) -> None:
    name = show(res["input"])
    if not res["ok"]:
        print(f"{name}: FAILED: {res['error']}", file=sys.stderr, flush=True)
        return
    if not opts.quiet:
        kind = "compact layout" if res["layout"] != "book" else (
            "book layout, chapters start new pages" if res["chapter_breaks"] else "book layout")
        pages = f"{res['pages']} page{'' if res['pages'] == 1 else 's'}"
        print(f"{name} -> {show(res['output'])}  ({pages}, {kind}, {res['seconds']:g} s)", flush=True)
    for w in res["warnings"]:
        print(f"{name}: ! {w}" if opts.quiet else f"  ! {w}", flush=True)
    linked = res["section_refs"]["linked"]
    if linked and not opts.quiet:
        print(f"  - {linked} numbered section reference{'' if linked == 1 else 's'} linked to headings "
              "(--no-section-links if they point into other documents)", flush=True)


async def convert_all(jobs: list, layout: Layout, opts: argparse.Namespace, home: pathlib.Path, edge: str) -> int:
    work = home / "tmp" / f"run-{os.getpid()}-{int(time.time() * 1000)}"
    work.mkdir(parents=True, exist_ok=True)
    mermaid, results = Mermaid(home), []
    browser = Browser(edge, work)
    try:
        await browser.start()
        for n, (src, out) in enumerate(jobs, 1):
            if not browser.alive():
                await browser.close()
                browser = Browser(edge, work)
                await browser.start()
            t0 = time.time()
            try:
                res = await convert_one(browser, src, out, layout, opts, work, n, mermaid)
            except SetupError:
                raise
            except Exception as e:  # noqa: BLE001 - report the failure and go on with the next file
                if os.environ.get("MD2PDF_DEBUG"):
                    import traceback
                    traceback.print_exc()
                msg = str(e) if isinstance(e, (RuntimeError, OSError)) and str(e) else f"{type(e).__name__}: {e}"
                res = {"input": str(src), "output": str(out), "ok": False, "error": msg}
            res["seconds"] = round(time.time() - t0, 1)
            results.append(res)
            if not opts.json:
                print_result(res, opts)
    finally:
        await browser.close()
        shutil.rmtree(work, ignore_errors=True)
    if opts.json:
        print(json.dumps(results, indent=1, ensure_ascii=True))
    return EXIT_OK if all(r["ok"] for r in results) else EXIT_FAILED


def plan_jobs(inputs: list[str], output: str | None) -> list[tuple[pathlib.Path, pathlib.Path]]:
    """Files, folders (their Markdown files) and wildcards -> [(markdown file, pdf file)]."""
    srcs: list[pathlib.Path] = []
    for raw in inputs:
        p = pathlib.Path(raw).expanduser()
        if p.is_file():
            if p.suffix.lower() == ".pdf":
                raise UsageError(f"{raw} is already a PDF; give the Markdown file")
            srcs.append(p)
        elif p.is_dir():
            found = sorted((q for q in p.iterdir() if q.is_file() and q.suffix.lower() in MD_SUFFIXES),
                           key=lambda q: q.name.lower())
            if not found:
                raise UsageError(f"no Markdown files in {raw}")
            srcs += found
        elif re.search(r"[*?\[]", raw):
            found = sorted((pathlib.Path(m) for m in glob.glob(os.path.expanduser(raw), recursive=True)
                            if os.path.isfile(m) and m.lower().endswith(MD_SUFFIXES)), key=lambda q: str(q).lower())
            if not found:
                raise UsageError(f"no Markdown files match {raw}")
            srcs += found
        else:
            raise UsageError(f"file not found: {raw}")
    files = list({os.path.normcase(str(s.resolve())): s.resolve() for s in srcs}.values())
    dest = pathlib.Path(output).expanduser() if output else None
    if dest is not None and dest.suffix.lower() == ".pdf":
        if len(files) > 1:
            raise UsageError(f"-o {output} names one PDF but there are {len(files)} input files; give a folder instead")
        return [(files[0], dest.resolve())]
    if dest is not None and dest.is_file():
        raise UsageError(f"-o {output} is a file, not a folder")
    jobs, used = [], set()
    for s in files:
        folder = dest.resolve() if dest is not None else s.parent
        pdf, k = folder / f"{s.stem}.pdf", 2
        while os.path.normcase(str(pdf)) in used:  # same file name from two folders into one output folder
            pdf, k = folder / f"{s.stem}-{k}.pdf", k + 1
        used.add(os.path.normcase(str(pdf)))
        jobs.append((s, pdf))
    return jobs


async def run_setup(home: pathlib.Path, edge: str, opts: argparse.Namespace, layout: Layout) -> int:
    from importlib import metadata
    versions = []
    for dist in ("markdown-it-py", "mdit-py-plugins", "pypdf", "websockets"):
        with contextlib.suppress(Exception):
            versions.append(f"{dist} {metadata.version(dist)}")
    print(f"md2pdf {VERSION}")
    print(f"  python    {sys.executable} ({sys.version.split()[0]})")
    print(f"  runtime   {home}")
    print(f"  packages  {', '.join(versions)}", flush=True)
    work = home / "tmp" / f"run-{os.getpid()}-setup"
    work.mkdir(parents=True, exist_ok=True)
    browser = Browser(edge, work)
    try:
        await browser.start()
        product = (await browser.cdp.send("Browser.getVersion", timeout=15)).get("product", "?")
        target, sid = await browser.new_tab()
        loaded = browser.cdp.expect("Page.loadEventFired", sid)
        await browser.cdp.send("Page.navigate", {"url": "data:text/html,<p>md2pdf</p>"}, session=sid)
        await asyncio.wait_for(loaded, 30)
        pdf = await print_pdf(browser.cdp, sid, layout)
        await browser.close_tab(target)
    finally:
        await browser.close()
        shutil.rmtree(work, ignore_errors=True)
    if not pdf.startswith(b"%PDF"):
        raise SetupError("Edge started but did not produce a PDF")
    print(f"  edge      {edge}")
    print(f"            {product}, headless printing works")
    if opts.mermaid:
        path, how = ensure_mermaid(home)
        print(f"  mermaid   {path} ({how})" if path else f"  mermaid   not available, diagrams will show as source ({how})")
    print("ready")
    return EXIT_OK


EPILOG = r"""examples:
  md2pdf notes.md                 writes notes.pdf next to notes.md
  md2pdf *.md -o C:\pdfs          every Markdown file in this folder, PDFs into C:\pdfs
  md2pdf docs -d rm2              the Markdown files in .\docs, laid out for a reMarkable 2
  md2pdf spec.md -o spec-a4.pdf -d a4 --layout compact

devices: paper-pro (default, 180 x 240 mm), rm2 (157.5 x 210 mm), move (91.8 x 163.2 mm),
         a5, a4, letter, or any WIDTHxHEIGHT such as 150x200mm or 6x8in

The first run installs md2pdf's Python packages into .runtime next to md2pdf.py (delete that folder
to uninstall). Documents are rendered on this PC by headless Microsoft Edge and are never uploaded.
mermaid.js is downloaded once, the first time a document has a mermaid diagram (--no-mermaid avoids it).
Windows launcher only: md2pdf --add-to-path adds the md2pdf folder to your user PATH.

exit codes: 0 done, 1 a file failed, 2 bad command line, 3 setup problem (Python packages, Edge)"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="md2pdf", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
                                description="Convert Markdown to a paginated PDF for a reMarkable tablet or for paper.")
    p.add_argument("inputs", nargs="*", metavar="INPUT", help="Markdown files, folders, or wildcards such as docs\\*.md")
    p.add_argument("-o", "--output", metavar="PATH", help="output PDF (one input) or folder (default: next to each input)")
    p.add_argument("-d", "--device", default="paper-pro", metavar="DEVICE", help="page size to lay out for (default: paper-pro)")
    p.add_argument("--layout", choices=("auto", "book", "compact"), default="auto",
                   help="book: cover page and a contents page with page numbers; compact: title on top of page 1. "
                        "auto (default) picks book for documents of 6+ pages with 4+ sections")
    p.add_argument("--breaks", choices=("auto", "on", "off"), default="auto",
                   help="start each top-level section on a new page (auto: in book layout unless it wastes many pages)")
    p.add_argument("--font-size", type=float, metavar="PT", help="body text size in points (default depends on the device)")
    p.add_argument("--no-section-links", dest="section_links", action="store_false",
                   help="do not link '\u00a74.2'-style references to the numbered headings of the same document")
    p.add_argument("--no-mermaid", dest="mermaid", action="store_false", help="show mermaid diagrams as source code")
    p.add_argument("--html", action="store_true", help="also save the rendered HTML next to each PDF")
    p.add_argument("--json", action="store_true", help="print a JSON report instead of text")
    p.add_argument("--browser", metavar="MSEDGE", help="path to Microsoft Edge (default: found automatically)")
    p.add_argument("--setup", action="store_true", help="install and check everything md2pdf needs, then exit")
    p.add_argument("-q", "--quiet", action="store_true", help="print only warnings and errors")
    p.add_argument("--version", action="version", version=f"md2pdf {VERSION}")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(errors="replace", line_buffering=True)
    parser = build_parser()
    opts = parser.parse_args(argv)
    if not opts.inputs and not opts.setup:
        parser.print_usage(sys.stderr)
        log("error: give one or more Markdown files, folders or wildcards (md2pdf --help for more)")
        return EXIT_USAGE
    try:
        layout = Layout(opts.device, opts.font_size)
        if not 6 <= layout.font <= 24:
            raise ValueError("--font-size must be between 6 and 24")
        jobs = [] if opts.setup else plan_jobs(opts.inputs, opts.output)
    except (ValueError, UsageError) as e:
        log(f"error: {e}")
        return EXIT_USAGE
    try:
        home = runtime_home()
        clean_stale(home)
        ensure_packages(home)
        edge = find_edge(opts.browser)
        if opts.setup:
            return asyncio.run(run_setup(home, edge, opts, layout))
        return asyncio.run(convert_all(jobs, layout, opts, home, edge))
    except SetupError as e:
        log(f"setup problem: {e}")
        return EXIT_SETUP
    except KeyboardInterrupt:
        log("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
