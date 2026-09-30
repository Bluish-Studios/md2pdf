"""The command line: choosing inputs and outputs, exit codes, printed results, and handling a file that fails."""
from __future__ import annotations

import argparse
import json
import os

import pytest

import md2pdf


# --- inputs and outputs -------------------------------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("a/one.md", "a/Two.markdown", "a/skip.txt", "b/one.md", "empty/.keep", "done.pdf"):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# x\n")
    return tmp_path


def names(jobs):
    return [(s.relative_to(s.parents[1]).as_posix(), d.name) for s, d in jobs]


def test_plan_jobs_files_folders_and_wildcards(tree):
    jobs = md2pdf.plan_jobs(["a", "b/one.md", "a/*.md"], None)
    assert names(jobs) == [("a/one.md", "one.pdf"), ("a/Two.markdown", "Two.pdf"), ("b/one.md", "one.pdf")]
    assert all(d.parent == s.parent for s, d in jobs)


def test_plan_jobs_into_one_folder_renames_clashes(tree):
    jobs = md2pdf.plan_jobs(["a/one.md", "b/one.md"], "out")
    assert [d.name for _, d in jobs] == ["one.pdf", "one-2.pdf"]
    assert all(d.parent == tree / "out" for _, d in jobs)


def test_plan_jobs_single_output_file(tree):
    [(src, dest)] = md2pdf.plan_jobs(["a/one.md"], "custom.pdf")
    assert dest == tree / "custom.pdf"


@pytest.mark.parametrize("inputs, output, message", [
    (["done.pdf"], None, "already a PDF"),
    (["empty"], None, "no Markdown files in empty"),
    (["nothing*.md"], None, "no Markdown files match"),
    (["missing.md"], None, "file not found"),
    (["a"], "x.pdf", "names one PDF but there are 2 input files"),
    (["a/one.md"], "a/skip.txt", "is a file, not a folder"),
])
def test_plan_jobs_errors(tree, inputs, output, message):
    with pytest.raises(md2pdf.UsageError, match=message):
        md2pdf.plan_jobs(inputs, output)


# --- exit codes ---------------------------------------------------------------------------------------------------

def test_main_without_inputs_prints_usage(capsys):
    assert md2pdf.main([]) == md2pdf.EXIT_USAGE
    assert "give one or more Markdown files" in capsys.readouterr().err


@pytest.mark.parametrize("args, message", [
    (["x.md", "-d", "kindle"], "unknown device"),
    (["x.md", "--font-size", "40"], "--font-size must be between 6 and 24"),
    (["missing.md"], "file not found"),
])
def test_main_usage_errors(tmp_path, monkeypatch, capsys, args, message):
    monkeypatch.chdir(tmp_path)
    assert md2pdf.main(args) == md2pdf.EXIT_USAGE
    assert message in capsys.readouterr().err


def test_main_bad_option_exits_2(capsys):
    with pytest.raises(SystemExit) as e:
        md2pdf.main(["--layout", "poster", "x.md"])
    assert e.value.code == 2


def test_main_version(capsys):
    with pytest.raises(SystemExit):
        md2pdf.main(["--version"])
    assert capsys.readouterr().out.strip() == f"md2pdf {md2pdf.VERSION}"


def test_main_setup_problem_exits_3(monkeypatch, capsys):
    def no_home():
        raise md2pdf.SetupError("no writable folder")
    monkeypatch.setattr(md2pdf, "runtime_home", no_home)
    assert md2pdf.main(["--setup"]) == md2pdf.EXIT_SETUP
    assert "setup problem: no writable folder" in capsys.readouterr().err


def test_main_interrupted(monkeypatch, capsys):
    def interrupt():
        raise KeyboardInterrupt
    monkeypatch.setattr(md2pdf, "runtime_home", interrupt)
    assert md2pdf.main(["--setup"]) == 130


# --- printed results ----------------------------------------------------------------------------------------------

def result(**overrides):
    base = {"input": os.path.abspath("notes.md"), "output": os.path.abspath("notes.pdf"), "ok": True, "pages": 1,
            "layout": "compact", "chapter_breaks": False, "seconds": 1.5, "warnings": [], "section_refs": {"linked": 0}}
    return {**base, **overrides}


def test_print_result_variants(capsys):
    quiet, loud = argparse.Namespace(quiet=True), argparse.Namespace(quiet=False)
    md2pdf.print_result(result(), loud)
    md2pdf.print_result(result(pages=9, layout="book", chapter_breaks=True, warnings=["w1"],
                               section_refs={"linked": 3}), loud)
    md2pdf.print_result(result(layout="book", warnings=["w2"]), quiet)
    md2pdf.print_result({"input": "bad.md", "ok": False, "error": "boom"}, loud)
    out, err = capsys.readouterr()
    assert "notes.md -> notes.pdf  (1 page, compact layout, 1.5 s)" in out
    assert "(9 pages, book layout, chapters start new pages" in out
    assert "  ! w1" in out and "3 numbered section references linked" in out
    assert "notes.md: ! w2" in out
    assert "bad.md: FAILED: boom" in err


def test_show_uses_relative_paths_inside_the_current_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert md2pdf.show(str(tmp_path / "a" / "b.md")) == os.path.join("a", "b.md")
    outside = str(tmp_path.parent / "elsewhere.md")
    assert md2pdf.show(outside) == outside


# --- one file failing does not stop the others --------------------------------------------------------------------

class FakeBrowser:
    started = 0

    def __init__(self, exe, work):
        self.exe, self.work, self.dead = exe, work, False

    async def start(self):
        FakeBrowser.started += 1
        return self

    def alive(self):
        return not self.dead

    async def close(self):
        pass


@pytest.fixture
def fake_run(monkeypatch, tmp_path):
    FakeBrowser.started = 0
    monkeypatch.setattr(md2pdf, "Browser", FakeBrowser)

    def run(behaviour, *args):
        async def convert_one(browser, src, out, layout, opts, work, n, mermaid):
            return behaviour(browser, src, out, n)
        monkeypatch.setattr(md2pdf, "convert_one", convert_one)
        opts = md2pdf.build_parser().parse_args(["x.md", *args])
        jobs = [(tmp_path / f"{n}.md", tmp_path / f"{n}.pdf") for n in (1, 2, 3)]
        import asyncio
        return asyncio.run(md2pdf.convert_all(jobs, md2pdf.Layout("a5", None), opts, tmp_path, "msedge"))
    return run


def ok(browser, src, out, n):
    return {**result(input=str(src), output=str(out)), "ok": True}


def test_convert_all_reports_a_failure_and_continues(fake_run, capsys):
    def behaviour(browser, src, out, n):
        if n == 2:
            raise ValueError("bad table")
        if n == 3:
            raise OSError("disk full")
        return ok(browser, src, out, n)
    assert fake_run(behaviour, "--json") == md2pdf.EXIT_FAILED
    records = json.loads(capsys.readouterr().out)
    assert [r["ok"] for r in records] == [True, False, False]
    assert records[1]["error"] == "ValueError: bad table" and records[2]["error"] == "disk full"
    assert all("seconds" in r for r in records)


def test_convert_all_prints_tracebacks_in_debug_mode(fake_run, monkeypatch, capsys):
    monkeypatch.setenv("MD2PDF_DEBUG", "1")

    def behaviour(browser, src, out, n):
        raise RuntimeError("page script failed")
    assert fake_run(behaviour) == md2pdf.EXIT_FAILED
    err = capsys.readouterr().err
    assert "Traceback" in err and "FAILED: page script failed" in err


def test_convert_all_restarts_a_crashed_browser(fake_run, capsys):
    def behaviour(browser, src, out, n):
        browser.dead = n == 1
        return ok(browser, src, out, n)
    assert fake_run(behaviour) == md2pdf.EXIT_OK
    assert FakeBrowser.started == 2


def test_convert_all_stops_on_setup_problems(fake_run, tmp_path):
    def behaviour(browser, src, out, n):
        raise md2pdf.SetupError("Edge vanished")
    with pytest.raises(md2pdf.SetupError):
        fake_run(behaviour)
    assert not any(p.name.startswith("run-") for p in (tmp_path / "tmp").iterdir())
