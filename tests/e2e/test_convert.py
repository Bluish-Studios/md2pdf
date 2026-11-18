"""Converts real documents with headless Edge and checks the PDFs: layout, contents, links, diagrams, page sizes.

Every PDF is kept in test-artifacts/ with a PNG of each page, so a person can look at what changed.
"""
from __future__ import annotations

import re

import pytest
from visual import ink, page_size_mm, page_texts

import md2pdf

pytestmark = pytest.mark.e2e


def contents_entries(text: str) -> list[tuple[str, int]]:
    """'Contents 1 Introduction 3 1.1 Goals 3 ...' -> [('1 Introduction', 3), ('1.1 Goals', 3), ...]."""
    body = text.split("Contents", 1)[1]
    return [(m.group(1).strip(), int(m.group(2))) for m in re.finditer(r"(\D+?|\d+(?:\.\d+)* \D+?)\s(\d+)(?=\s|$)", body)]


def test_showcase_book_layout(docs, run_json, keep):
    code, [rec] = run_json(docs / "showcase.md", "-o", docs / "showcase.pdf", "--layout", "book")
    assert code == md2pdf.EXIT_OK and rec["ok"], rec
    pdf = docs / "showcase.pdf"
    pages = keep(pdf)

    assert rec["layout"] == "book" and rec["warnings"] == []
    assert rec["mermaid"] == {"count": 2, "failed": 0, "errors": []}
    assert rec["section_refs"] == {"linked": 6, "plain": 1}
    assert 5 <= rec["pages"] <= 10 and len(pages) == rec["pages"]
    assert rec["bookmarks"] >= 14 and rec["internal_links"] >= 10 and rec["external_links"] >= 2
    assert all(ink(p) > 0.001 for p in pages), "a page came out blank"

    reader = md2pdf.PdfReader(str(pdf))
    w, h = page_size_mm(reader.pages[0])
    assert abs(w - 180) < 1 and abs(h - 240) < 1

    texts = page_texts(pdf)
    assert "md2pdf Showcase" in texts[0] and "Example Author" in texts[0]
    assert "One document that uses every feature" in texts[0]
    assert texts[1].startswith("2 / ") and "Contents" in texts[1]

    # Every contents entry points at the page where that heading really is.
    entries = contents_entries(texts[1])
    assert len(entries) >= 12, entries
    for title, page in entries:
        label = re.sub(r"^\d+(?:\.\d+)*\s", "", title)
        assert label in texts[page - 1], f"contents says {title!r} is on page {page}"

    everything = " ".join(texts)
    for expected in ("Note", "Tip", "Warning", "Caution", "Details are printed expanded", "Footnotes are collected",
                     "Appendix", "Button tags are removed but their text stays."):
        assert expected in everything
    for unsafe in ("unsafe", "alert(1)", "onclick", "javascript:"):
        assert unsafe not in everything


def test_short_note_compact_on_a4_with_html(docs, run_json, keep):
    code, [rec] = run_json(docs / "short-note.md", "-d", "a4", "--html")
    assert code == md2pdf.EXIT_OK
    keep(docs / "short-note.pdf")
    assert rec["layout"] == "compact" and rec["pages"] == 1
    assert rec["warnings"] == ["image not found: img/not-there.png", "link to a heading that does not exist: #does-not-exist"]
    w, h = page_size_mm(md2pdf.PdfReader(rec["output"]).pages[0])
    assert abs(w - 210) < 1 and abs(h - 297) < 1
    html = (docs / "short-note.html").read_text(encoding="utf-8")
    assert "<script" not in html and re.search(r'<body class="[^"]*\bcompact\b', html)


def bookmarks(pdf) -> list[tuple[str, int]]:
    """(PDF bookmark title, page number), as Edge wrote them."""
    reader, found = md2pdf.PdfReader(str(pdf)), []

    def walk(items):
        for it in items:
            if isinstance(it, list):
                walk(it)
            else:
                found.append((" ".join(it.title.split()), reader.get_destination_page_number(it) + 1))
    walk(reader.outline)
    return found


def undouble(title: str) -> str:
    half = len(title) // 2
    return title[:half] if len(title) % 2 == 0 and title[:half] == title[half:] else title


def outline_pages(pdf) -> dict[str, int]:
    """Bookmark title -> page, ignoring the doubled titles of test_bookmark_titles_are_not_doubled."""
    return {undouble(title): page for title, page in bookmarks(pdf)}


def assert_chapters_start_pages(pdf, chapters: list[str]):
    texts, where = page_texts(pdf), outline_pages(pdf)
    pages = [where[c] for c in chapters]
    assert pages == sorted(set(pages)), f"chapters share pages: {dict(zip(chapters, pages, strict=True))}"
    for chapter, page in zip(chapters, pages, strict=True):
        body = re.sub(r"^\d+ / \d+", "", texts[page - 1])
        assert chapter in body[:120], f"{chapter!r} is not at the top of page {page}: {body[:120]!r}"


SHOWCASE_CHAPTERS = ["1. Introduction", "2. Formatting", "3. Diagrams", "4. Alerts and HTML", "5. Images and links"]


def test_small_device_book_layout_with_chapter_breaks(docs, run_json, keep):
    code, [rec] = run_json(docs / "showcase.md", "-d", "move", "--layout", "book", "--breaks", "on", "-o", docs / "move.pdf")
    assert code == md2pdf.EXIT_OK and rec["warnings"] == []
    keep(docs / "move.pdf")
    assert rec["layout"] == "book" and rec["chapter_breaks"] is True and rec["font_pt"] == 9.5
    w, h = page_size_mm(md2pdf.PdfReader(rec["output"]).pages[0])
    assert abs(w - 91.8) < 1 and abs(h - 163.2) < 1
    assert_chapters_start_pages(docs / "move.pdf", SHOWCASE_CHAPTERS)


def test_long_document_picks_book_layout_by_itself(tmp_path, run_json, keep):
    paragraph = ("This paragraph is filler text for layout testing. It is long enough to wrap over several lines, "
                 "so that each chapter fills most of a page and the whole document is clearly longer than six pages. ")
    chapters = [f"{n}. Chapter {n}" for n in range(1, 9)]
    body = "".join(f"# {c}\n\n" + "".join(f"{paragraph * 2}\n\n" for _ in range(6)) + f"## {c[0]}.1 Detail\n\n{paragraph}\n\n"
                   for c in chapters)
    src = tmp_path / "long.md"
    src.write_text("---\ntitle: A Long Document\n---\n" + body, encoding="utf-8")
    code, [rec] = run_json(src)
    assert code == md2pdf.EXIT_OK and rec["warnings"] == []
    keep(tmp_path / "long.pdf")
    assert rec["layout"] == "book" and rec["chapter_breaks"] is True
    assert rec["pages"] >= 10 and rec["passes"] >= 2
    assert_chapters_start_pages(tmp_path / "long.pdf", chapters)
    # The contents page lists each chapter with the page Edge actually put it on.
    contents, where = page_texts(tmp_path / "long.pdf")[1], outline_pages(tmp_path / "long.pdf")
    for n, chapter in enumerate(chapters, 1):
        assert f"{n} Chapter {n} {where[chapter]}" in contents


@pytest.mark.xfail(strict=True, reason="known bug: Edge repeats the bookmark title of a heading that starts a page, "
                                        "e.g. 'ContentsContents'. Remove this mark once md2pdf fixes the outline.")
def test_bookmark_titles_are_not_doubled(docs, run_json):
    code, _ = run_json(docs / "showcase.md", "--layout", "book", "--breaks", "on", "-o", docs / "marks.pdf")
    assert code == md2pdf.EXIT_OK
    doubled = [title for title, _ in bookmarks(docs / "marks.pdf") if undouble(title) != title]
    assert doubled == []


@pytest.mark.xfail(strict=True, reason="known bug: every mermaid diagram gets the SVG id 'mermaid-0', so a second diagram's "
                                        "styles and markers collide with the first. Remove this mark once md2pdf fixes it.")
def test_mermaid_diagrams_get_unique_svg_ids(docs, run_json):
    code, [rec] = run_json(docs / "showcase.md", "--html", "-o", docs / "ids.pdf")
    assert code == md2pdf.EXIT_OK and rec["mermaid"]["count"] == 2
    ids = re.findall(r'<svg[^>]*\sid="([^"]+)"', (docs / "ids.html").read_text(encoding="utf-8"))
    assert len(ids) == 2 and len(set(ids)) == 2, ids


def test_folder_input_to_output_folder_without_mermaid(docs, tmp_path, run_json, keep):
    out = tmp_path / "out"
    code, recs = run_json(docs, "-o", out, "--no-mermaid", "-d", "rm2")
    assert code == md2pdf.EXIT_OK
    assert sorted(r["output"] for r in recs) == [str(out / "short-note.pdf"), str(out / "showcase.pdf")]
    showcase = next(r for r in recs if r["output"].endswith("showcase.pdf"))
    assert showcase["mermaid"] is None
    keep(out / "showcase.pdf", "folder_no_mermaid")
    assert "A[Markdown] --> B[HTML]" in " ".join(page_texts(out / "showcase.pdf"))


def test_custom_page_size_and_font(docs, run_json):
    code, [rec] = run_json(docs / "short-note.md", "-d", "6x8in", "--font-size", "13", "-o", docs / "custom.pdf")
    assert code == md2pdf.EXIT_OK and rec["font_pt"] == 13
    w, h = page_size_mm(md2pdf.PdfReader(str(docs / "custom.pdf")).pages[0])
    assert abs(w - 152.4) < 1 and abs(h - 203.2) < 1


def test_broken_mermaid_is_shown_as_source(tmp_path, run_json, keep):
    src = tmp_path / "broken.md"
    src.write_text("# Broken diagram\n\n```mermaid\ngraph LR\n  A -->\n```\n", encoding="utf-8")
    code, [rec] = run_json(src)
    assert code == md2pdf.EXIT_OK and rec["ok"]
    keep(tmp_path / "broken.pdf")
    assert rec["mermaid"]["failed"] == 1
    assert rec["warnings"][0].startswith("mermaid diagram shown as source:")
    assert "A -->" in page_texts(tmp_path / "broken.pdf")[0]


def test_mermaid_unavailable(tmp_path, monkeypatch, run_json):
    monkeypatch.setenv("MD2PDF_HOME", str(tmp_path / "fresh-home"))
    monkeypatch.setenv("MD2PDF_MERMAID_URL", str(tmp_path / "missing" / "mermaid.min.js"))
    src = tmp_path / "diagram.md"
    src.write_text("# D\n\n```mermaid\ngraph LR\n  A --> B\n```\n", encoding="utf-8")
    code, [rec] = run_json(src)
    assert code == md2pdf.EXIT_OK
    [warning] = rec["warnings"]
    assert warning.startswith("1 mermaid diagram(s) shown as source: mermaid.js could not be downloaded (")
    assert rec["mermaid"]["failed"] == 1


def test_text_output_and_a_failed_file(docs, tmp_path, run_cli):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "short-note.pdf").mkdir()  # a folder where the PDF should go: writing it fails
    code, out, err = run_cli(docs / "short-note.md", docs / "showcase.md", "-o", locked)
    assert code == md2pdf.EXIT_FAILED
    assert "short-note.md: FAILED:" in err
    assert "showcase.pdf" in out and "pages" in out


def test_setup_check(run_cli):
    code, out, err = run_cli("--setup")
    assert code == md2pdf.EXIT_OK
    assert "headless printing works" in out and out.strip().endswith("ready")
    assert "packages  markdown-it-py" in out
