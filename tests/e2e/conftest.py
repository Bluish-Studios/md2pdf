"""End-to-end fixtures: skip without Edge (except in CI), and keep each PDF with page images for review."""
from __future__ import annotations

import os
import pathlib
import re
import shutil

import pytest
from visual import artifacts_dir, contact_sheet, render_pages

import md2pdf


def edge_available() -> bool:
    try:
        md2pdf.find_edge(None)
        return True
    except md2pdf.SetupError:
        return False


def pytest_collection_modifyitems(config, items):
    if edge_available() or os.environ.get("CI"):
        return  # in CI a missing Edge must fail, not skip
    skip = pytest.mark.skip(reason="Microsoft Edge is not installed")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def keep(request):
    """keep(pdf) copies the PDF into test-artifacts/<test name>/ with page PNGs and a contact sheet; returns the pages."""
    def save(pdf: pathlib.Path, label: str | None = None):
        dest = artifacts_dir() / re.sub(r"[^\w.-]+", "_", label or request.node.name)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf, dest / pdf.name)
        pages = render_pages(pdf)
        for n, img in enumerate(pages, 1):
            img.save(dest / f"{pdf.stem}-page-{n:02}.png")
        contact_sheet(pages).save(dest / f"{pdf.stem}-all-pages.png")
        return pages
    return save
