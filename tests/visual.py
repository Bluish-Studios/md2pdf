"""Helpers for looking at PDFs: page images, contact sheets, page sizes and text."""
from __future__ import annotations

import os
import pathlib

import md2pdf

ROOT = pathlib.Path(__file__).resolve().parent.parent
MM = 72 / 25.4  # PDF points per millimetre


def artifacts_dir() -> pathlib.Path:
    """Where the end-to-end tests keep their PDFs and page previews for people to look at (CI uploads it)."""
    return pathlib.Path(os.environ.get("MD2PDF_TEST_ARTIFACTS") or ROOT / "test-artifacts")


def render_pages(pdf: pathlib.Path, scale: float = 1.5):
    import pypdfium2
    doc = pypdfium2.PdfDocument(str(pdf))
    try:
        return [page.render(scale=scale).to_pil().convert("RGB") for page in doc]
    finally:
        doc.close()


def contact_sheet(images, columns: int = 4, gap: int = 12):
    from PIL import Image, ImageDraw
    w, h = max(i.width for i in images), max(i.height for i in images)
    rows = -(-len(images) // columns)
    cols = min(columns, len(images))
    sheet = Image.new("RGB", (cols * (w + gap) + gap, rows * (h + gap) + gap), (96, 96, 96))
    draw = ImageDraw.Draw(sheet)
    for n, img in enumerate(images):
        x, y = gap + (n % columns) * (w + gap), gap + (n // columns) * (h + gap)
        sheet.paste(img, (x, y))
        draw.text((x + 6, y + 4), str(n + 1), fill=(200, 0, 0))
    return sheet


def ink(img) -> float:
    """Share of pixels that are not near-white: 0 for a blank page."""
    gray = img.convert("L").resize((200, int(200 * img.height / img.width)))
    hist = gray.histogram()
    return sum(hist[:200]) / (gray.width * gray.height)


def page_size_mm(page) -> tuple[float, float]:
    box = page.mediabox
    return float(box.width) / MM, float(box.height) / MM


def page_texts(pdf: pathlib.Path) -> list[str]:
    reader = md2pdf.PdfReader(str(pdf))
    return [" ".join((p.extract_text() or "").split()) for p in reader.pages]
