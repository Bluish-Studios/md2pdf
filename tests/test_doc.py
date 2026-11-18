"""Doc: front matter, titles, heading anchors, links, section references, contents, and the HTML md2pdf prints."""
from __future__ import annotations

import re

import md2pdf


def render(doc: md2pdf.Doc) -> str:
    return doc.md.renderer.render(doc.body, doc.md.options, doc.env)


# --- titles and front matter --------------------------------------------------------------------------------------

def test_single_h1_becomes_the_title_and_leaves_the_body(make_doc):
    doc = make_doc("# My Title\n\n## Part A\n\ntext\n")
    assert doc.title == "My Title"
    assert doc.title_head is not None
    assert [h["text"] for h in doc.headings] == ["Part A"]
    assert "My Title" not in render(doc)


def test_front_matter_meta_and_mismatched_title(make_doc):
    doc = make_doc("---\ntitle: 'Official Name'\nauthor: Someone\ndate: 2026-01-01\n---\n# Different\n\ntext\n")
    assert doc.meta == {"title": "Official Name", "author": "Someone", "date": "2026-01-01"}
    assert doc.title == "Official Name"
    assert doc.title_head is None  # the H1 stays in the body as a heading
    assert "Different" in render(doc)


def test_title_falls_back_to_the_file_name(make_doc):
    doc = make_doc("# One\n\n# Two\n", name="Getting-Started.md")
    assert doc.title_head is None and doc.title == "Getting Started"
    assert make_doc("just text\n", name="notes.md").title == "notes"


def test_blockquote_after_title_becomes_the_lede(make_doc):
    doc = make_doc("# T\n\n> The summary.\n> Two lines.\n\n## A\n\nbody\n")
    lede = doc.md.renderer.render(doc.lede, doc.md.options, doc.env)
    assert "The summary." in lede and "The summary." not in render(doc)


# --- headings -----------------------------------------------------------------------------------------------------

def test_heading_ids_are_unique_and_levels_are_relative(make_doc):
    doc = make_doc("# T\n\n## Intro\n\n### Detail\n\n## Intro\n\n#### Deep\n")
    ids = [h["id"] for h in doc.headings]
    assert ids == ["intro", "detail", "intro-1", "deep"]
    html = render(doc)
    assert '<h2 id="intro" class="r0">' in html and '<h3 id="detail" class="r1">' in html
    assert '<h4 id="deep" class="r2">' in html
    assert html.count(md2pdf.CHAPTER_MARK) == 2


def test_nested_headings_do_not_start_chapters(make_doc):
    doc = make_doc("# A\n\n> # Quoted\n\n# B\n")
    assert doc.title_head is None
    assert render(doc).count(md2pdf.CHAPTER_MARK) == 2


def test_html_ids_are_link_targets(make_doc):
    doc = make_doc('# T\n\n<a id="custom-spot"></a>\n\n[go](#custom-spot) [go2](#Custom%20Spot)\n')
    html = render(doc)
    assert html.count('href="#custom-spot"') == 2


# --- links --------------------------------------------------------------------------------------------------------

def test_link_classification(make_doc):
    doc = make_doc("# T\n\n## Part One\n\nx\n")
    assert doc.link("https://e.com") == ("https://e.com", "ext")
    assert doc.link("mailto:a@b.c") == ("mailto:a@b.c", "ext")
    assert doc.link("vscode://open") == (None, "plain")
    assert doc.link("other.md#x") == (None, "docref")
    assert doc.link("doc.md#part-one") == ("#part-one", "xref")
    assert doc.link("doc.md") == (None, "docref")  # a link to this whole file has nowhere to jump to
    assert doc.link("#PART ONE") == ("#part-one", "xref")
    assert doc.link("#nope") == (None, "plain")
    assert doc.link("#!!!") == (None, "plain")
    assert doc.report["broken_anchors"] == ["#nope", "#!!!"]
    assert doc.link(r"C:\x\y.md") == (None, "docref")


def test_unfollowable_links_keep_their_text(make_doc):
    html = render(make_doc("# T\n\nSee [the other doc](other.md) and [site](https://e.com).\n"))
    assert '<span class="docref">the other doc</span>' in html
    assert '<a href="https://e.com" class="ext">site</a>' in html


def test_bare_urls_become_links(make_doc):
    html = render(make_doc("# T\n\nVisit https://example.com/a_b. Done.\n"))
    assert '<a class="url" href="https://example.com/a_b">' in html
    assert "</a>. Done." in html


# --- § section references -----------------------------------------------------------------------------------------

SECTIONS = "---\ntitle: Doc\n---\n# 1. Intro\n\n## 1.1 Scope\n\n# 2. Design\n\n"


def test_section_refs_link_to_numbered_headings(make_doc):
    doc = make_doc(SECTIONS + "See §1.1, §2 and §9.\n")
    html = render(doc)
    assert '<a class="xref" href="#11-scope">§1.1</a>' in html
    assert '<a class="xref" href="#2-design">§2</a>' in html
    assert "§9" in html and 'href="#9' not in html
    assert (doc.report["refs_linked"], doc.report["refs_plain"]) == (2, 1)


def test_section_refs_into_other_files_stay_plain(make_doc):
    doc = make_doc(SECTIONS + "As design.md §1.1, §2 says.\n")
    assert doc.report == {**doc.report, "refs_linked": 0, "refs_plain": 2}


def test_section_links_can_be_turned_off(make_doc):
    doc = make_doc(SECTIONS + "See §2.\n", section_links=False)
    assert 'class="xref"' not in render(doc)


def test_section_refs_inside_links_are_left_alone(make_doc):
    html = render(make_doc(SECTIONS + "[see §2](https://e.com)\n"))
    assert '<a href="https://e.com" class="ext">see §2</a>' in html


# --- contents -----------------------------------------------------------------------------------------------------

def test_toc_numbered(make_doc):
    doc = make_doc(SECTIONS + "### 2.1.1 Too deep for the contents\n")
    assert [h["id"] for h in doc.toc_entries()] == ["1-intro", "11-scope", "2-design"]
    toc = doc.toc_html()
    assert '<nav class="toc">' in toc and '<span class="n">1.1</span>' in toc and 'data-id="2-design"' in toc


def test_toc_unnumbered_and_empty(make_doc):
    assert '<nav class="toc nonum">' in make_doc("# T\n\n## A\n\n## B\n").toc_html()
    empty = make_doc("just text\n")
    assert empty.toc_entries() == [] and empty.toc_html() == "" and empty.inline_toc() == ""


def test_inline_toc_marker(make_doc):
    html = render(make_doc("# T\n\n[[_TOC_]]\n\n## A\n\n## B\n"))
    assert '<nav class="inline-toc">' in html and 'href="#a"' in html


# --- alerts, lists, images and code -------------------------------------------------------------------------------

def test_github_alerts(make_doc):
    html = render(make_doc("# T\n\nIntro.\n\n> [!WARNING]\n> Careful.\n\n> [!note] Inline\n\n> Plain quote\n"))
    assert '<blockquote class="alert alert-warning">' in html
    assert '<span class="alert-title">Warning</span>' in html
    assert '<span class="alert-title">Note</span>Inline' in html
    assert "<blockquote>\n<p>Plain quote" in html


def test_task_lists_and_footnotes(make_doc):
    html = render(make_doc("# T\n\n- [x] done\n- [ ] todo\n\nText.[^n]\n\n[^n]: The note.\n"))
    assert html.count("task-list-item-checkbox") == 2 and html.count("disabled checked>") == 1
    assert "footnotes" in html


def test_images(make_doc, tmp_path):
    (tmp_path / "pic.png").write_bytes(b"x")
    doc = make_doc('# T\n\n![A *pic*](pic.png "Caption") ![gone](gone.png)\n')
    html = render(doc)
    assert 'alt="A pic"' in html and 'title="Caption"' in html and "file:///" in html
    assert "[image not found: gone.png]" in html and doc.report["missing_images"] == ["gone.png"]


def test_code_fences_and_mermaid(make_doc):
    text = "# T\n\n```js\nx() // c\n```\n\n    indented\n\n```mermaid\ngraph LR\n```\n"
    doc = make_doc(text)
    html = render(doc)
    assert doc.mermaid_count == 1
    assert '<span class="cm">// c</span>' in html and "indented" in html
    assert '<figure class="mermaid-block"><div class="mermaid-src">graph LR' in html
    plain = make_doc(text, name="plain.md", mermaid=False)
    assert plain.mermaid_count == 0 and "mermaid-block" not in render(plain)


def test_inline_code_wraps(make_doc):
    assert "<code>a/<wbr>b</code>" in render(make_doc("# T\n\n`a/b`\n"))


# --- the full page ------------------------------------------------------------------------------------------------

def test_build_html_fills_every_placeholder(make_doc, tmp_path):
    doc = make_doc("---\nauthor: A. Person\ndescription: What it is.\n---\n# Title <b>x</b>\n\nintro\n\n## One\n\n## Two\n")
    js = tmp_path / "mermaid.min.js"
    html = md2pdf.build_html(doc, "auto", "auto", js)
    assert not re.search(r"__[A-Z]+__", html)
    assert "@page {\n  size: 180mm 240mm;" in html
    assert '<p class="lede">What it is.</p>' in html and "A. Person" in html
    assert '<section class="preamble">' in html and html.count('<section class="chapter"') == 2
    assert "<script" not in html  # no diagrams, so mermaid.js is not loaded
    assert 'class="layout-auto breaks-auto"' in html


def test_build_html_loads_mermaid_only_when_needed(make_doc, tmp_path):
    doc = make_doc("# T\n\n```mermaid\ngraph LR\n```\n")
    js = tmp_path / "mermaid.min.js"
    assert f'<script src="{js.as_uri()}"></script>' in md2pdf.build_html(doc, "book", "on", js)
    assert "<script" not in md2pdf.build_html(doc, "book", "on", None)


def test_footer_title_is_shortened_and_escaped_for_css(make_doc):
    doc = make_doc('# A "quoted" x < y ' + "word " * 40 + "\n")
    assert md2pdf.footer_title(doc).endswith("\u2026")
    css_line = next(ln for ln in md2pdf.build_html(doc, "auto", "auto", None).splitlines() if "@bottom-left" in ln)
    assert '\\"quoted\\"' in css_line and "x \\3C  y" in css_line
