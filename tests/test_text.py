"""Pure functions: text handling, Azure DevOps preprocessing, HTML sanitizing, code blocks and page geometry."""
from __future__ import annotations

import pathlib

import pytest

import md2pdf


# --- escaping and wrapping ---------------------------------------------------------------------------------------

def test_esc_and_attr():
    assert md2pdf.esc('<a href="x">&</a>') == '&lt;a href="x"&gt;&amp;&lt;/a&gt;'
    assert md2pdf.attr('say "hi"') == "say &quot;hi&quot;"


def test_with_breaks_adds_wrap_points_after_separators():
    assert md2pdf.with_breaks("a/b") == "a/<wbr>b"
    assert md2pdf.with_breaks("x<y") == "x&lt;y"


def test_with_breaks_splits_dotted_names_and_long_camel_case():
    assert "System.<wbr>IO" in md2pdf.with_breaks("System.IO")
    assert md2pdf.with_breaks("SomeVeryLongIdentifierName") == "Some<wbr>Very<wbr>Long<wbr>Identifier<wbr>Name"
    assert "<wbr>" not in md2pdf.with_breaks("ShortName")


def test_with_breaks_for_urls():
    assert md2pdf.with_breaks("https://a.b/c?d=e", url=True) == "https:/<wbr>/<wbr>a.<wbr>b/<wbr>c?<wbr>d=<wbr>e"


# --- reading files ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("data", [
    "é\r\nx".encode(),
    b"\xef\xbb\xbf" + "é\r\nx".encode(),
    "é\r\nx".encode("utf-16"),
    "é\rx".encode("cp1252"),
])
def test_read_text_handles_encodings_and_line_endings(tmp_path, data):
    p = tmp_path / "f.md"
    p.write_bytes(data)
    assert md2pdf.read_text(p) == "é\nx"


# --- Azure DevOps wiki syntax -------------------------------------------------------------------------------------

def test_preprocess_converts_ado_mermaid_block():
    out = md2pdf.preprocess("text\n  ::: mermaid\n  graph LR\n  :::\nafter")
    assert out == "text\n  ```mermaid\n  graph LR\n  ```\nafter"


def test_preprocess_closes_an_unterminated_mermaid_block():
    assert md2pdf.preprocess(":::mermaid\ngraph LR").endswith("```")


def test_preprocess_toc_markers():
    assert md2pdf.TOC_MARK in md2pdf.preprocess("[[_TOC_]]")
    assert md2pdf.TOC_MARK in md2pdf.preprocess("[TOC]")
    assert md2pdf.preprocess("a\n[[_TOSP_]]\nb") == "a\nb"


@pytest.mark.parametrize("src, expected", [
    ("#Heading", "# Heading"),
    ("##Sub heading", "## Sub heading"),
    ("# Already fine", "# Already fine"),
    ("#include <stdio.h>", "#include <stdio.h>"),
    ("#123 is a work item", "#123 is a work item"),
])
def test_preprocess_ado_headings(src, expected):
    assert md2pdf.preprocess(src) == expected


def test_preprocess_image_sizes():
    assert md2pdf.preprocess("![a](p.png =300x)") == '<img src="p.png" alt="a" width="300">'
    assert md2pdf.preprocess("![a](p.png =x200)") == '<img src="p.png" alt="a" height="200">'
    assert md2pdf.preprocess("![a](<p.png> =10x20)") == '<img src="p.png" alt="a" width="10">'


def test_preprocess_leaves_fenced_code_alone():
    # A shorter fence inside a longer one does not close it; a closing fence may be longer than the opening one.
    text = "````\n#Heading\n[[_TOC_]]\n```\nstill code\n`````\n#After"
    out = md2pdf.preprocess(text).split("\n")
    assert out[1:5] == ["#Heading", "[[_TOC_]]", "```", "still code"]
    assert out[-1] == "# After"


def test_preprocess_tilde_fences():
    assert md2pdf.preprocess("~~~\n#x\n~~~\n#y") == "~~~\n#x\n~~~\n# y"


# --- names, anchors and paths -------------------------------------------------------------------------------------

@pytest.mark.parametrize("stem, expected", [
    ("Getting-Started", "Getting Started"),
    ("Pre%2Dflight-Checks", "Pre-flight Checks"),
    ("already spaced-name", "already spaced-name"),
    ("plain", "plain"),
])
def test_humanize(stem, expected):
    assert md2pdf.humanize(stem) == expected


def test_slugify_and_loose():
    assert md2pdf.slugify("2.1 Tables & Code!") == "21-tables--code"
    assert md2pdf.slugify("???") == "section"
    assert md2pdf.loose("My%20Heading-1") == "myheading1"


def test_resolve_src_remote_and_data():
    assert md2pdf.resolve_src("https://x/y.png", pathlib.Path(".")) == ("https://x/y.png", "remote")
    assert md2pdf.resolve_src("data:image/png;base64,AA", pathlib.Path("."))[1] == "remote"


def test_resolve_src_local_relative_and_encoded(tmp_path):
    img = tmp_path / "my pic.png"
    img.write_bytes(b"x")
    assert md2pdf.resolve_src("my%20pic.png?raw=1#frag", tmp_path) == (img.resolve().as_uri(), "local")
    assert md2pdf.resolve_src(img.resolve().as_uri(), tmp_path) == (img.resolve().as_uri(), "local")


def test_resolve_src_wiki_rooted_walks_up(tmp_path):
    img = tmp_path / ".attachments" / "a.png"
    img.parent.mkdir()
    img.write_bytes(b"x")
    deep = tmp_path / "wiki" / "section"
    deep.mkdir(parents=True)
    assert md2pdf.resolve_src("/.attachments/a.png", deep) == (img.resolve().as_uri(), "local")


def test_resolve_src_missing(tmp_path):
    assert md2pdf.resolve_src("nope.png", tmp_path) == (None, "missing")
    assert md2pdf.resolve_src("", tmp_path) == (None, "missing")
    assert md2pdf.resolve_src("//server/share/x.png", tmp_path) == (None, "missing")


# --- HTML sanitizing ----------------------------------------------------------------------------------------------

class FakeDoc:
    def __init__(self, base):
        self.base = base
        self.report = {"missing_images": [], "broken_anchors": []}

    def link(self, href):
        return (href, "ext") if href.startswith("http") else (None, "plain")


@pytest.fixture
def fake_doc(tmp_path):
    return FakeDoc(tmp_path)


def test_sanitize_removes_scripts_forms_and_handlers(fake_doc):
    out = md2pdf.sanitize_html('<script>x()</script><form><input name=a></form><b onclick="x()" class="k">hi</b>', fake_doc)
    assert out == '<b class="k">hi</b>'


def test_sanitize_drops_script_urls_and_srcset(fake_doc):
    out = md2pdf.sanitize_html('<a href="javascript:x()">t</a><img srcset="a.png 2x" src="data:image/png;base64,AA">', fake_doc)
    assert out == '<a>t</a><img src="data:image/png;base64,AA">'


def test_sanitize_keeps_comments_and_escapes_unknown_tags(fake_doc):
    assert md2pdf.sanitize_html("<!-- c --><custom-tag>", fake_doc) == "<!-- c -->&lt;custom-tag&gt;"


def test_sanitize_checkbox_inputs(fake_doc):
    assert "checked" in md2pdf.sanitize_html('<input type="checkbox" checked>', fake_doc)
    assert "checked" not in md2pdf.sanitize_html("<input type=checkbox>", fake_doc)
    assert md2pdf.sanitize_html('<input type="text">', fake_doc) == ""


def test_sanitize_opens_details_and_keeps_bare_attributes(fake_doc):
    assert md2pdf.sanitize_html("<details>", fake_doc) == "<details open>"
    assert md2pdf.sanitize_html("<details open>", fake_doc) == "<details open>"
    assert md2pdf.sanitize_html("<br/>", fake_doc) == "<br />"
    assert md2pdf.sanitize_html("</div>", fake_doc) == "</div>"


def test_sanitize_resolves_images_and_links(fake_doc, tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    out = md2pdf.sanitize_html('<img src="a.png" alt=\'A\'>', fake_doc)
    assert (tmp_path / "a.png").resolve().as_uri() in out and 'alt="A"' in out
    missing = md2pdf.sanitize_html('<img src="gone.png">', fake_doc)
    assert "image not found: gone.png" in missing and fake_doc.report["missing_images"] == ["gone.png"]
    assert md2pdf.sanitize_html('<a href="https://e.com" title=t>x</a>', fake_doc) == '<a href="https://e.com" title="t">x</a>'
    assert md2pdf.sanitize_html('<a href="other.md">x</a>', fake_doc) == "<a>x</a>"


# --- code blocks --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("line, lang, comment", [
    ("x = 1 // note", "js", "// note"),
    ("x = 1  # note", "python", "# note"),
    ("#!/bin/sh", "bash", None),
    ("url = 'http://x'", "js", None),
    ("# plain", "text", None),
])
def test_code_line_comments(line, lang, comment):
    out = md2pdf.code_line(line, lang)
    if comment:
        assert f'<span class="cm">{comment}</span>' in out
    else:
        assert "cm" not in out


def test_code_html_shrinks_then_wraps(layout):
    short = md2pdf.code_html("print(1)\n", "python", layout)
    assert "nowrap" in short and "font-size:8.5pt" in short
    long = md2pdf.code_html("x" * 400, "text", layout)
    assert 'class="code wrap"' in long
    medium = md2pdf.code_html("y" * 100, "python", layout)
    size = float(medium.split("font-size:")[1].split("pt")[0])
    assert 7.3 <= size < 8.5 and "nowrap" in medium
    assert '<span class="ln"> </span>' in md2pdf.code_html("\n", "", layout)


# --- page geometry ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("device", list(md2pdf.DEVICES))
def test_layout_known_devices(device):
    lay = md2pdf.Layout(device, None)
    label, w, h, _, font = md2pdf.DEVICES[device]
    assert (lay.label, lay.w, lay.h, lay.font) == (label, w, h, font)
    assert 0 < lay.content_w_mm < w and 0 < lay.content_h_mm < h


@pytest.mark.parametrize("spec, w, h, font", [
    ("150x200", 150, 200, 11.0),
    ("15x20cm", 150, 200, 11.0),
    ("6x8in", 152.4, 203.2, 11.0),
    ("120x160mm", 120, 160, 10.0),
    ("100x160mm", 100, 160, 9.5),
])
def test_layout_custom_sizes(spec, w, h, font):
    lay = md2pdf.Layout(spec, None)
    assert (round(lay.w, 1), round(lay.h, 1), lay.font) == (w, h, font)
    assert lay.label.endswith("mm page")


def test_layout_errors_and_font_override():
    with pytest.raises(ValueError, match="unknown device"):
        md2pdf.Layout("kindle", None)
    with pytest.raises(ValueError, match="out of range"):
        md2pdf.Layout("10x10mm", None)
    lay = md2pdf.Layout("a4", 14)
    assert lay.font == 14 and lay.scale == pytest.approx(14 / 11)


def test_print_params_are_in_inches():
    p = md2pdf.Layout("letter", None).print_params()
    assert p["paperWidth"] == pytest.approx(8.5) and p["paperHeight"] == pytest.approx(11)
    assert p["preferCSSPageSize"] and p["generateDocumentOutline"]
