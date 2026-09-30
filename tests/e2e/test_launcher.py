"""The Windows launchers from a fresh copy: first-run install, a conversion, and exit codes through cmd -> PowerShell -> Python."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
from visual import ROOT

pytestmark = [pytest.mark.launcher, pytest.mark.e2e,
              pytest.mark.skipif(os.name != "nt", reason="md2pdf.cmd and md2pdf.ps1 are the Windows launchers")]


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    """A copy of just the three shipped scripts, with its own empty runtime folder, set up once for the module."""
    folder = tmp_path_factory.mktemp("fresh-copy")
    for name in ("md2pdf.cmd", "md2pdf.ps1", "md2pdf.py"):
        shutil.copy2(ROOT / name, folder / name)
    env = {**os.environ, "MD2PDF_HOME": str(folder / "home"), "MD2PDF_PYTHON": sys.executable}
    env.pop("MD2PDF_LAUNCHER", None)
    return folder, env


def run(fresh, *args, launcher="md2pdf.cmd", **env_overrides):
    folder, env = fresh
    env = {**env, **env_overrides}
    if launcher == "md2pdf.cmd":
        cmd = ["cmd.exe", "/d", "/c", str(folder / "md2pdf.cmd"), *args]
    else:
        cmd = ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(folder / launcher), *args]
    return subprocess.run(cmd, cwd=folder, env=env, capture_output=True, text=True, timeout=600)


def test_first_run_setup_installs_packages(fresh):
    folder, _ = fresh
    res = run(fresh, "--setup", "--no-mermaid")
    assert res.returncode == 0, res.stderr
    assert "first run for this Python: installing 4 packages" in res.stderr
    assert res.stdout.strip().endswith("ready")
    [lib] = (folder / "home").glob("lib-py*")
    assert (lib / ".complete").is_file()
    assert not list(folder.glob(".runtime"))  # MD2PDF_HOME was honoured


def test_launchers_convert_and_report_json(fresh, tmp_path):
    src = tmp_path / "hello.md"
    src.write_text("# Hello\n\nFrom the launcher test.\n", encoding="utf-8")
    res = run(fresh, str(src), "--json", "--no-mermaid")
    assert res.returncode == 0, res.stderr
    [rec] = json.loads(res.stdout)
    assert rec["ok"] and rec["pages"] == 1 and (tmp_path / "hello.pdf").is_file()
    res = run(fresh, str(src), "-o", str(tmp_path / "via-ps1.pdf"), "-q", launcher="md2pdf.ps1")
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "via-ps1.pdf").read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize("launcher", ["md2pdf.cmd", "md2pdf.ps1"])
def test_exit_codes_pass_through(fresh, launcher):
    assert run(fresh, launcher=launcher).returncode == 2  # no inputs
    assert run(fresh, "missing.md", launcher=launcher).returncode == 2
    bad = run(fresh, "--setup", launcher=launcher, MD2PDF_PYTHON=str(fresh[0] / "no-python.exe"))
    assert bad.returncode == 3 and "is not Python 3.10 or newer" in bad.stderr
