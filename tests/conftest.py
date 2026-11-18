"""Shared fixtures. The tests import md2pdf.py directly and use the packages from requirements-dev.txt."""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import md2pdf  # noqa: E402
from visual import artifacts_dir  # noqa: E402

md2pdf.load_deps()


def pytest_sessionstart(session):
    shutil.rmtree(artifacts_dir(), ignore_errors=True)


@pytest.fixture
def opts():
    """Parsed command-line options with the defaults, as a Doc or convert_one expects them."""
    return md2pdf.build_parser().parse_args(["x.md"])


@pytest.fixture
def layout():
    return md2pdf.Layout("paper-pro", None)


@pytest.fixture
def make_doc(tmp_path, layout, opts):
    """make_doc(markdown, name='doc.md', **option overrides) -> Doc for a file in tmp_path."""
    def make(text: str, name: str = "doc.md", **overrides) -> md2pdf.Doc:
        src = tmp_path / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(text, encoding="utf-8")
        for key, value in overrides.items():
            setattr(opts, key, value)
        return md2pdf.Doc(src.resolve(), layout, opts)
    return make


def write_png(path: pathlib.Path, size=(120, 60), color=(28, 61, 102)) -> pathlib.Path:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


@pytest.fixture
def docs(tmp_path) -> pathlib.Path:
    """A copy of tests/fixtures in tmp_path/docs, with the images the showcase links to."""
    dest = tmp_path / "docs"
    shutil.copytree(FIXTURES, dest)
    write_png(dest / "img" / "chart.png")
    write_png(tmp_path / ".attachments" / "badge.png", size=(40, 16), color=(200, 60, 60))
    return dest


@pytest.fixture(scope="session")
def runtime_home(tmp_path_factory) -> pathlib.Path:
    """One MD2PDF_HOME for the whole session, so mermaid.js is downloaded at most once."""
    return tmp_path_factory.mktemp("md2pdf-home")


@pytest.fixture
def run_cli(monkeypatch, capsys, runtime_home):
    """run_cli(*args) -> (exit code, stdout, stderr), running md2pdf.main in this process.

    Packages come from the test environment instead of a pip install into the runtime folder; the launcher tests
    cover the real first-run install.
    """
    monkeypatch.setenv("MD2PDF_HOME", str(runtime_home))
    monkeypatch.setattr(md2pdf, "ensure_packages", lambda home: md2pdf.load_deps())

    def run(*args: str):
        code = md2pdf.main([str(a) for a in args])
        out, err = capsys.readouterr()
        return code, out, err
    return run


@pytest.fixture
def run_json(run_cli):
    """run_json(*args) -> (exit code, list of per-file records)."""
    def run(*args: str):
        code, out, err = run_cli(*args, "--json")
        assert out.strip(), f"no JSON on stdout; stderr was:\n{err}"
        return code, json.loads(out)
    return run
