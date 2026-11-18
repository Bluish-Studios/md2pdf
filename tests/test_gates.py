"""scripts/gates.py: a gate that misfires gets bypassed, so its precision is tested like any other code."""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gates  # noqa: E402

TEMPLATE = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
MARK = "TO" + "DO"  # spelled in two parts so this file passes the gate it tests
# Personal-looking addresses are assembled at run time, so this file passes the privacy gate it tests.
AT = "@"
GMAIL, CORP, REALMAIL = "first.last" + AT + "gmail.com", "person" + AT + "company.com", "ada" + AT + "realmail.io"
NESTED, REAL = "someone" + AT + "corp.example.net.au", "real" + AT + "person.org"


def filled(**sections: str) -> str:
    """The PR template with each placeholder section replaced by real text (or by the given overrides)."""
    body = {"What": "Adds X.", "Why": "Because Y.", "How": "Changed md2pdf.py.", "Testing": "- [x] check.ps1 passes",
            "Task record": "docs/tasks/TASK_X.md", "New dependency": "None", "Manual intervention": "None",
            "Open questions": "None"}
    body.update(sections)
    return "\n\n".join(f"## {k}\n\n{v}" for k, v in body.items())


# --- privacy ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("email, allowed", [
    ("12345+someone@users.noreply.github.com", True),
    ("noreply@github.com", True),
    ("noreply@anthropic.com", True),
    ("dependabot[bot]@users.noreply.github.com", True),
    ("someone@example.com", True),
    (GMAIL, False),
    (CORP, False),
])
def test_email_allowlist(email, allowed):
    assert bool(gates.ALLOWED_EMAIL.match(email)) is allowed


def test_scan_text_flags_emails_and_local_patterns():
    patterns = [re.compile(r"ada\s+lovelace", re.I)]
    problems = gates.scan_text("f.md", f"ok line\ncontact {REALMAIL}\nby Ada  Lovelace\nsee x@example.com", patterns)
    assert problems == [f"f.md:2: personal email address '{REALMAIL}' (use a noreply or example.com address)",
                        "f.md:3: matches a pattern in .pii-patterns.local (use the [User] placeholder)"]


def test_pii_patterns_file(tmp_path, monkeypatch):
    monkeypatch.setattr(gates, "PII_FILE", tmp_path / ".pii-patterns.local")
    assert gates.pii_patterns() == []
    (tmp_path / ".pii-patterns.local").write_text("# comment\n\nAda\\s+Lovelace\n", encoding="utf-8")
    [p] = gates.pii_patterns()
    assert p.search("ADA LOVELACE")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway git repository that the gates run against."""
    # Inside a git hook, GIT_INDEX_FILE and friends point at the real repository; never let them leak in here.
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "1+test@users.noreply.github.com")
    git("config", "commit.gpgsign", "false")
    monkeypatch.setattr(gates, "ROOT", tmp_path)
    monkeypatch.setattr(gates, "PII_FILE", tmp_path / ".pii-patterns.local")
    return tmp_path, git


def test_privacy_staged_tracked_and_history(repo):
    root, git = repo
    (root / "a.md").write_text("clean\n", encoding="utf-8")
    git("add", "a.md")
    git("commit", "-q", "-m", "clean")
    assert gates.gate_privacy(staged=False, history=True) == []

    (root / "a.md").write_text(f"clean\nmail me: {NESTED}\n", encoding="utf-8")
    git("add", "a.md")
    assert gates.gate_privacy(staged=True, history=False) == [
        f"a.md:1: personal email address '{NESTED}' (use a noreply or example.com address)"]

    git("config", "user.email", REAL)
    assert any(f"git config user.email is '{REAL}'" in p for p in gates.gate_privacy(staged=True, history=False))
    git("commit", "-q", "-m", "leak")
    history = gates.gate_privacy(staged=False, history=True)
    assert any(f"author email '{REAL}' is not a noreply address" in p for p in history)
    assert any(p.startswith("a.md:2: personal email") for p in history)


# --- TODO links ---------------------------------------------------------------------------------------------------

def test_todos_need_an_issue_link(repo):
    root, git = repo
    (root / "ok.py").write_text(f"# {MARK} #12 tidy this\n# {MARK}: https://github.com/o/r/issues/3\n", encoding="utf-8")
    (root / "bad.ps1").write_text(f"# FIX{'ME'} later\n", encoding="utf-8")
    (root / "notes.md").write_text(f"# {MARK} in Markdown is not checked\n", encoding="utf-8")
    (root / "prose.py").write_text(f"# the {MARK} links gate checks every {MARK}/FIX{'ME'} marker\n", encoding="utf-8")
    git("add", ".")
    [problem] = gates.gate_todos()
    assert problem.startswith("bad.ps1:1:")


def test_repository_has_no_unlinked_todos():
    assert gates.gate_todos() == []


# --- PR description -----------------------------------------------------------------------------------------------

def test_the_unfilled_template_fails():
    problems = gates.gate_pr_body(TEMPLATE, None)
    for name in ("What", "Why", "How", "Task record"):
        assert f"section '## {name}' is empty or still has only the template placeholder" in problems
    assert "section '## Testing' has no ticked item and no description of what was tested" in problems
    assert not any("New dependency" in p or "Manual intervention" in p or "Open questions" in p for p in problems[:-1])


def test_a_filled_description_passes():
    assert gates.gate_pr_body(filled(), None) == []


@pytest.mark.parametrize("sections, message", [
    ({"What": "None"}, "section '## What' cannot be 'None'"),
    ({"Testing": "- [ ] not done"}, "section '## Testing' has no ticked item"),
    ({"Manual intervention": "Run the script by hand."}, "must be 'None' or link the tracking issue"),
])
def test_bad_sections(sections, message):
    assert any(message in p for p in gates.gate_pr_body(filled(**sections), None))


def test_missing_section():
    assert "missing section '## Why' (see .github/pull_request_template.md)" in gates.gate_pr_body(
        filled().replace("## Why", "## Reason"), None)


def test_manual_intervention_with_an_issue_passes():
    assert gates.gate_pr_body(filled(**{"Manual intervention": "#7 turn on branch protection"}), None) == []


def test_placeholders_and_comments_do_not_count_as_content():
    sections = gates.pr_sections("## A\n\n<!-- hidden\ntext -->\n_Link the `TASK_<NAME>.md`_\n- [ ] _item_\n## B\nreal\n")
    assert sections == {"A": "", "B": "real"}


def test_dependency_changes_need_a_justification(repo):
    root, git = repo
    (root / "requirements-dev.txt").write_text("pytest>=8\n", encoding="utf-8")
    (root / "md2pdf.py").write_text('REQUIREMENTS = ["a"]\n', encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD").strip()

    (root / "requirements-dev.txt").write_text("pytest>=8\n# a comment only\n", encoding="utf-8")
    git("commit", "-qam", "comment")
    assert not gates.dependency_changed(base)
    assert gates.gate_pr_body(filled(), base) == []

    (root / "md2pdf.py").write_text('REQUIREMENTS = ["a", "b"]\n', encoding="utf-8")
    git("commit", "-qam", "new runtime dependency")
    assert gates.dependency_changed(base)
    assert any("justify it under '## New dependency'" in p for p in gates.gate_pr_body(filled(), base))
    assert gates.gate_pr_body(filled(**{"New dependency": "b 1.x: parses Y; stdlib cannot; maintained by Z"}), base) == []


# --- command line -------------------------------------------------------------------------------------------------

def test_main_reports_and_exits(tmp_path, capsys):
    body = tmp_path / "body.md"
    body.write_text(filled(), encoding="utf-8")
    assert gates.main(["pr-body", "--body-file", str(body)]) == 0
    assert capsys.readouterr().out.strip() == "pr-body: OK"
    body.write_text(TEMPLATE, encoding="utf-8")
    assert gates.main(["pr-body", "--body-file", str(body)]) == 1
    assert "pr-body: section '## What'" in capsys.readouterr().err
    assert gates.main(["todos"]) == 0
