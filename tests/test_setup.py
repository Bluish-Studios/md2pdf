"""First-run setup: the runtime folder, the private package install, mermaid.js, and finding and starting Edge."""
from __future__ import annotations

import os
import pathlib
import sys
import time
import types

import pytest

import md2pdf

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_dev_requirements_match_runtime_requirements():
    lines = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    pinned = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
    assert pinned[:len(md2pdf.REQUIREMENTS)] == md2pdf.REQUIREMENTS


# --- runtime folder -----------------------------------------------------------------------------------------------

def test_writable_dir(tmp_path):
    assert md2pdf.writable_dir(tmp_path / "new" / "dir")
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert not md2pdf.writable_dir(blocker / "sub")


def test_runtime_home_uses_md2pdf_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MD2PDF_HOME", str(tmp_path / "home"))
    assert md2pdf.runtime_home() == (tmp_path / "home").resolve()


def test_runtime_home_rejects_an_unwritable_md2pdf_home(tmp_path, monkeypatch):
    (tmp_path / "file").write_text("x")
    monkeypatch.setenv("MD2PDF_HOME", str(tmp_path / "file" / "home"))
    with pytest.raises(md2pdf.SetupError, match="MD2PDF_HOME is not writable"):
        md2pdf.runtime_home()


def test_runtime_home_defaults_next_to_the_script(tmp_path, monkeypatch):
    monkeypatch.delenv("MD2PDF_HOME", raising=False)
    monkeypatch.setattr(md2pdf, "TOOL_DIR", tmp_path)
    assert md2pdf.runtime_home() == tmp_path / ".runtime"


def test_runtime_home_falls_back_to_localappdata(tmp_path, monkeypatch):
    monkeypatch.delenv("MD2PDF_HOME", raising=False)
    monkeypatch.setattr(md2pdf, "TOOL_DIR", tmp_path / "readonly")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    real = md2pdf.writable_dir
    monkeypatch.setattr(md2pdf, "writable_dir", lambda p: "readonly" not in str(p) and real(p))
    assert md2pdf.runtime_home() == tmp_path / "appdata" / "md2pdf"
    monkeypatch.setattr(md2pdf, "writable_dir", lambda p: False)
    with pytest.raises(md2pdf.SetupError, match="no writable folder"):
        md2pdf.runtime_home()


def test_clean_stale_removes_only_old_runs(tmp_path):
    old, new = tmp_path / "tmp" / "run-old", tmp_path / "tmp" / "run-new"
    old.mkdir(parents=True)
    new.mkdir()
    day_ago = time.time() - 2 * 86400
    os.utime(old, (day_ago, day_ago))
    md2pdf.clean_stale(tmp_path)
    assert not old.exists() and new.exists()


# --- package install ----------------------------------------------------------------------------------------------

@pytest.fixture
def fake_pip(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if run.code == 0:
            pathlib.Path(cmd[cmd.index("--target") + 1]).mkdir(parents=True, exist_ok=True)
        return types.SimpleNamespace(returncode=run.code, stdout=run.output)
    run.code, run.output = 0, ""
    monkeypatch.setattr(md2pdf.subprocess, "run", run)
    monkeypatch.setattr(md2pdf, "load_deps", lambda: None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return run, calls


def lib_dir(home: pathlib.Path) -> pathlib.Path:
    import hashlib
    key = hashlib.sha1("\n".join(md2pdf.REQUIREMENTS).encode()).hexdigest()[:8]
    return home / f"lib-py{sys.version_info[0]}{sys.version_info[1]}-{key}"


def test_ensure_packages_installs_once_and_removes_old_versions(tmp_path, fake_pip):
    run, calls = fake_pip
    stale = tmp_path / f"lib-py{sys.version_info[0]}{sys.version_info[1]}-00000000"
    stale.mkdir()
    md2pdf.ensure_packages(tmp_path)
    lib = lib_dir(tmp_path)
    assert len(calls) == 1 and "--target" in calls[0] and str(lib) in calls[0]
    assert (lib / ".complete").read_text(encoding="utf-8").splitlines() == md2pdf.REQUIREMENTS
    assert not stale.exists() and sys.path[0] == str(lib)
    md2pdf.ensure_packages(tmp_path)
    assert len(calls) == 1  # already installed


def test_ensure_packages_reports_pip_failures(tmp_path, fake_pip):
    run, _ = fake_pip
    run.code, run.output = 1, "/usr/bin/python: No module named pip"
    with pytest.raises(md2pdf.SetupError, match="MD2PDF_PYTHON=private"):
        md2pdf.ensure_packages(tmp_path)


def test_ensure_packages_reinstalls_after_a_broken_import(tmp_path, fake_pip, monkeypatch):
    def broken():
        raise ImportError("no module named websockets")
    monkeypatch.setattr(md2pdf, "load_deps", broken)
    with pytest.raises(md2pdf.SetupError, match="failed to import"):
        md2pdf.ensure_packages(tmp_path)
    assert not (lib_dir(tmp_path) / ".complete").exists()


# --- mermaid.js ---------------------------------------------------------------------------------------------------

FAKE_MERMAID = b"/* mermaid */" + b" " * 120_000


def test_ensure_mermaid_from_a_local_file_then_cached(tmp_path, monkeypatch):
    src = tmp_path / "mermaid.min.js"
    src.write_bytes(FAKE_MERMAID)
    monkeypatch.setenv("MD2PDF_MERMAID_URL", str(src))
    path, how = md2pdf.ensure_mermaid(tmp_path / "home")
    assert how == "downloaded" and path.read_bytes() == FAKE_MERMAID
    assert md2pdf.ensure_mermaid(tmp_path / "home") == (path, "cached")


def test_ensure_mermaid_downloads(tmp_path, monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return FAKE_MERMAID
    monkeypatch.delenv("MD2PDF_MERMAID_URL", raising=False)
    seen = []
    monkeypatch.setattr(md2pdf.urllib.request, "urlopen", lambda url, timeout: seen.append(url) or Response())
    path, how = md2pdf.ensure_mermaid(tmp_path)
    assert how == "downloaded" and seen == md2pdf.MERMAID_URLS[:1]


def test_ensure_mermaid_rejects_bad_content_and_reports_every_source(tmp_path, monkeypatch):
    src = tmp_path / "tiny.js"
    src.write_bytes(b"not it")
    monkeypatch.setenv("MD2PDF_MERMAID_URL", str(src))
    path, why = md2pdf.ensure_mermaid(tmp_path / "home")
    assert path is None and "unexpected content" in why


def test_mermaid_is_fetched_at_most_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(md2pdf, "ensure_mermaid", lambda home: calls.append(home) or (None, "offline"))
    m = md2pdf.Mermaid(tmp_path)
    assert m.get() is None and m.get() is None
    assert len(calls) == 1 and m.why == "mermaid.js could not be downloaded (offline)"


# --- finding Edge -------------------------------------------------------------------------------------------------

def fake_exe(folder: pathlib.Path, name: str = "msedge.exe") -> pathlib.Path:
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / name
    exe.write_bytes(b"")
    return exe


def test_find_edge_explicit_file_and_folder(tmp_path):
    exe = fake_exe(tmp_path)
    assert md2pdf.find_edge(str(exe)) == str(exe)
    if os.name == "nt":
        assert md2pdf.find_edge(str(tmp_path)) == str(exe)


def test_find_edge_explicit_errors(tmp_path, monkeypatch):
    with pytest.raises(md2pdf.SetupError, match="not found"):
        md2pdf.find_edge(str(tmp_path / "missing.exe"))
    chrome = fake_exe(tmp_path, "chrome.exe")
    monkeypatch.setenv("MD2PDF_EDGE", str(chrome))
    with pytest.raises(md2pdf.SetupError, match="not Microsoft Edge"):
        md2pdf.find_edge(None)


@pytest.mark.skipif(os.name != "nt", reason="Windows install locations")
def test_find_edge_standard_locations(tmp_path, monkeypatch):
    monkeypatch.delenv("MD2PDF_EDGE", raising=False)
    for var in ("ProgramFiles(x86)", "ProgramFiles"):
        monkeypatch.setenv(var, str(tmp_path / "none"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    exe = fake_exe(tmp_path / "local" / "Microsoft" / "Edge Beta" / "Application")
    assert md2pdf.find_edge(None) == str(exe)


def test_find_edge_on_path_and_not_found(tmp_path, monkeypatch):
    monkeypatch.delenv("MD2PDF_EDGE", raising=False)
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "none"))
    monkeypatch.setattr(md2pdf.shutil, "which", lambda name: "/opt/edge/msedge" if name == "microsoft-edge" else None)
    assert md2pdf.find_edge(None) == "/opt/edge/msedge"
    monkeypatch.setattr(md2pdf.shutil, "which", lambda name: None)
    with pytest.raises(md2pdf.SetupError, match="Microsoft Edge was not found"):
        md2pdf.find_edge(None)


# --- Edge policies and launching ----------------------------------------------------------------------------------

@pytest.fixture
def fake_registry(monkeypatch):
    """A winreg stand-in: values[(hive, name)] = data."""
    values: dict = {}
    reg = types.SimpleNamespace(HKEY_LOCAL_MACHINE="HKLM", HKEY_CURRENT_USER="HKCU")

    class Key:
        def __init__(self, hive):
            self.hive = hive

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def open_key(hive, path):
        if not any(h == hive for h, _ in values):
            raise OSError("no key")
        return Key(hive)

    def query(key, name):
        if (key.hive, name) not in values:
            raise OSError("no value")
        return values[(key.hive, name)], 4
    reg.OpenKey, reg.QueryValueEx = open_key, query
    monkeypatch.setitem(sys.modules, "winreg", reg)
    monkeypatch.setattr(md2pdf.os, "name", "nt")
    return values


def test_edge_policies(fake_registry):
    assert md2pdf.edge_policies() == {}
    fake_registry[("HKCU", "RemoteDebuggingAllowed")] = 0
    fake_registry[("HKCU", "HeadlessModeEnabled")] = 1  # allowed
    found = md2pdf.edge_policies()
    assert list(found) == ["RemoteDebuggingAllowed"] and "HKCU\\SOFTWARE\\Policies" in found["RemoteDebuggingAllowed"]


def test_edge_policies_elsewhere(monkeypatch):
    monkeypatch.setattr(md2pdf.os, "name", "posix")
    assert md2pdf.edge_policies() == {}


def test_launch_edge_refuses_when_remote_debugging_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(md2pdf, "edge_policies", lambda: {"RemoteDebuggingAllowed": "turned off by policy"})
    with pytest.raises(md2pdf.SetupError, match="cannot drive Edge on this PC: turned off by policy"):
        md2pdf.launch_edge("msedge", tmp_path)


class FakeProc:
    def __init__(self, code=None):
        self.code, self.killed = code, False

    def poll(self):
        return self.code

    def kill(self):
        self.killed = True


def test_launch_edge_reads_the_devtools_port(tmp_path, monkeypatch):
    monkeypatch.setattr(md2pdf, "edge_policies", lambda: {})
    monkeypatch.setenv("__COMPAT_LAYER", "DetectorsAppHealth")

    def popen(cmd, **kwargs):
        profile = pathlib.Path(next(a for a in cmd if a.startswith("--user-data-dir=")).split("=", 1)[1])
        profile.mkdir(parents=True)
        (profile / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n", encoding="utf-8")
        assert "--headless" in cmd and "--disable-background-networking" in cmd
        assert "__COMPAT_LAYER" not in kwargs["env"] and "PATH" in {k.upper() for k in kwargs["env"]}
        return FakeProc()
    monkeypatch.setattr(md2pdf.subprocess, "Popen", popen)
    proc, url, profile = md2pdf.launch_edge("msedge", tmp_path)
    assert url == "ws://127.0.0.1:9222/devtools/browser/abc" and profile == tmp_path / "edge-profile"


def test_launch_edge_reports_an_early_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(md2pdf, "edge_policies", lambda: {"HeadlessModeEnabled": "headless mode is turned off by policy"})
    proc = FakeProc(code=5)
    monkeypatch.setattr(md2pdf.subprocess, "Popen", lambda cmd, **kw: proc)
    with pytest.raises(md2pdf.SetupError, match="headless mode is turned off"):
        md2pdf.launch_edge("msedge", tmp_path)
    assert proc.killed
    monkeypatch.setattr(md2pdf, "edge_policies", lambda: {})
    with pytest.raises(md2pdf.SetupError, match="exited with code 5"):
        md2pdf.launch_edge("msedge", tmp_path)


def test_launch_edge_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(md2pdf, "edge_policies", lambda: {})
    monkeypatch.setattr(md2pdf.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    clock = iter([0, 0, 31, 31])
    monkeypatch.setattr(md2pdf.time, "time", lambda: next(clock))
    monkeypatch.setattr(md2pdf.time, "sleep", lambda s: None)
    with pytest.raises(md2pdf.SetupError, match="did not open its DevTools port"):
        md2pdf.launch_edge("msedge", tmp_path)


# --- writing files ------------------------------------------------------------------------------------------------

def test_write_file_is_atomic_and_explains_locked_files(tmp_path, monkeypatch):
    out = tmp_path / "sub" / "a.pdf"
    md2pdf.write_file(out, b"%PDF")
    assert out.read_bytes() == b"%PDF" and [p.name for p in out.parent.iterdir()] == ["a.pdf"]

    def locked(src, dst):
        raise PermissionError("in use")
    monkeypatch.setattr(md2pdf.os, "replace", locked)
    with pytest.raises(RuntimeError, match="is it open in another program"):
        md2pdf.write_file(out, b"%PDF-2")
    assert [p.name for p in out.parent.iterdir()] == ["a.pdf"]


def test_as_path(tmp_path):
    assert md2pdf.as_path((tmp_path / "a b.png").as_uri()) == str(tmp_path / "a b.png")
    assert md2pdf.as_path("https://x/y.png") == "https://x/y.png"
