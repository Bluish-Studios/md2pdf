#!/usr/bin/env python3
"""Repository gates that are not a linter or a test: privacy, TODO links, and the PR description.

  python scripts/gates.py privacy [--staged] [--history]   personal names/emails in files and commit metadata
  python scripts/gates.py todos                            every TODO/FIXME marker in code links an issue
  python scripts/gates.py pr-body --body-file F [--base REF]  the PR description follows the template

Each gate prints what is wrong and exits 1, or prints a one-line OK and exits 0. Standard library only.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PII_FILE = ROOT / ".pii-patterns.local"  # untracked: the patterns it holds must never enter the repo

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")
# Addresses that identify nobody: GitHub's private commit addresses, bots, and documentation domains.
ALLOWED_EMAIL = re.compile(r"(?i)^(?:[\w.+-]+@users\.noreply\.github\.com|noreply@github\.com|noreply@anthropic\.com"
                           r"|[\w.+-]+\[bot\]@users\.noreply\.github\.com|[\w.+-]+@example\.(?:com|org|net)|git@github\.com)$")
# A marker, not prose that mentions one: the word right after a comment sign (#, //, ::, rem), or followed by ":" or
# "(" anywhere. The words are built in two parts so this file does not flag itself.
_WORD = r"(?:TO" r"DO|FIX" r"ME)"
MARKER_RE = re.compile(rf"(?:#|//|::|\b[Rr][Ee][Mm]\b)\s*{_WORD}\b|\b{_WORD}\s*[:(]")
ISSUE_REF_RE = re.compile(r"#\d+\b|github\.com/[\w.-]+/[\w.-]+/issues/\d+")
CODE_SUFFIXES = (".py", ".ps1", ".psd1", ".cmd", ".yml", ".yaml", ".toml")

PR_SECTIONS = ["What", "Why", "How", "Testing", "Task record", "New dependency", "Manual intervention", "Open questions"]
NONE_ALLOWED = {"Task record", "New dependency", "Manual intervention", "Open questions"}
# A template placeholder: a whole line in _italics_, optionally as a list item or checkbox.
PLACEHOLDER_RE = re.compile(r"\s*(?:[-*]\s*(?:\[[ xX]\]\s*)?)?_.*_\s*")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True,  # noqa: S603, S607 - git only
                          encoding="utf-8", errors="replace").stdout


def pii_patterns() -> list[re.Pattern]:
    if not PII_FILE.is_file():
        return []
    lines = PII_FILE.read_text(encoding="utf-8").splitlines()
    return [re.compile(ln.strip(), re.I) for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def scan_text(label: str, text: str, patterns: list[re.Pattern]) -> list[str]:
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        for email in EMAIL_RE.findall(line):
            if not ALLOWED_EMAIL.match(email):
                problems.append(f"{label}:{n}: personal email address {email!r} (use a noreply or example.com address)")
        for p in patterns:
            if p.search(line):
                problems.append(f"{label}:{n}: matches a pattern in .pii-patterns.local (use the [User] placeholder)")
    return problems


def staged_additions() -> list[tuple[str, str]]:
    """(file, added text) for each file in the staged diff."""
    out, current, added = [], None, []
    for line in git("diff", "--cached", "--unified=0", "--no-color", "--diff-filter=ACMR").splitlines():
        if line.startswith("+++ "):
            if current:
                out.append((current, "\n".join(added)))
            current, added = line[6:] if line.startswith("+++ b/") else None, []
        elif line.startswith("+") and current:
            added.append(line[1:])
    if current:
        out.append((current, "\n".join(added)))
    return out


def tracked_files() -> list[str]:
    return [f for f in git("ls-files", "-z").split("\0") if f]


def gate_privacy(staged: bool, history: bool) -> list[str]:
    patterns, problems = pii_patterns(), []
    if staged:
        for name, text in staged_additions():
            problems += scan_text(name, text, patterns)
            problems += scan_text(f"file name {name}", name, patterns)
        email = git("config", "user.email").strip()
        if email and not ALLOWED_EMAIL.match(email):
            problems.append(f"git config user.email is {email!r}: commits would carry it. Use your GitHub noreply address "
                            "(git config user.email <id>+<login>@users.noreply.github.com)")
    else:
        for name in tracked_files():
            path = ROOT / name
            if path.is_file() and not name.endswith((".png", ".pdf", ".ico")):
                problems += scan_text(name, path.read_text(encoding="utf-8", errors="replace"), patterns)
    if history:
        log = git("log", "--all", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e")
        for rec in filter(str.strip, log.split("\x1e")):
            sha, an, ae, cn, ce, body = rec.strip().split("\x00", 5)
            for who, email in (("author", ae), ("committer", ce)):
                if not ALLOWED_EMAIL.match(email):
                    problems.append(f"commit {sha[:8]}: {who} email {email!r} is not a noreply address")
            problems += scan_text(f"commit {sha[:8]} message", f"{an}\n{cn}\n{body}", patterns)
    return problems


def gate_todos() -> list[str]:
    problems = []
    for name in tracked_files():
        if not name.endswith(CODE_SUFFIXES) or not (ROOT / name).is_file():
            continue
        for n, line in enumerate((ROOT / name).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if MARKER_RE.search(line) and not ISSUE_REF_RE.search(line):
                problems.append(f"{name}:{n}: a TO{'DO'}/FIX{'ME'} marker must link an issue (#123 or its URL): {line.strip()}")
    return problems


def pr_sections(body: str) -> dict[str, str]:
    """'## Heading' -> its text, with HTML comments and _italic placeholder_ lines removed."""
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    sections, current = {}, None
    for line in body.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            current = m.group(1)
            sections[current] = ""
        elif current is not None and not PLACEHOLDER_RE.fullmatch(line):
            sections[current] += line + "\n"
    return {k: v.strip() for k, v in sections.items()}


def testing_is_filled(text: str) -> bool:
    """At least one ticked box, or something other than unticked boxes."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return any(re.match(r"[-*]\s*\[[xX]\]", ln) or not re.match(r"[-*]\s*\[ \]", ln) for ln in lines)


def changed_lines(base: str, path: str) -> list[str]:
    diff = git("diff", f"{base}...HEAD", "--unified=0", "--no-color", "--", path)
    return [ln[1:].strip() for ln in diff.splitlines() if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))]


def dependency_changed(base: str) -> bool:
    """True when the PR edits a requirement line, or the REQUIREMENTS list md2pdf installs on first run."""
    if any(ln and not ln.startswith("#") for ln in changed_lines(base, "requirements-dev.txt")):
        return True
    return any(ln.startswith("REQUIREMENTS") for ln in changed_lines(base, "md2pdf.py"))


def gate_pr_body(body: str, base: str | None) -> list[str]:
    sections, problems = pr_sections(body), []
    for name in PR_SECTIONS:
        text = sections.get(name)
        if text is None:
            problems.append(f"missing section '## {name}' (see .github/pull_request_template.md)")
        elif not text:
            problems.append(f"section '## {name}' is empty or still has only the template placeholder")
        elif re.match(r"(?i)^none\b", text) and name not in NONE_ALLOWED:
            problems.append(f"section '## {name}' cannot be 'None'")
        elif name == "Testing" and not testing_is_filled(text):
            problems.append("section '## Testing' has no ticked item and no description of what was tested")
    manual = sections.get("Manual intervention") or ""
    if manual and not re.match(r"(?i)^none\b", manual) and not ISSUE_REF_RE.search(manual):
        problems.append("'## Manual intervention' must be 'None' or link the tracking issue (#123) with the exact steps")
    if base and dependency_changed(base) and re.match(r"(?i)^none\b", sections.get("New dependency") or "none"):
        problems.append("this PR changes a dependency (requirements-dev.txt or REQUIREMENTS in md2pdf.py): "
                        "justify it under '## New dependency' (what, why, alternatives, maintainer/licence)")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="gate", required=True)
    pv = sub.add_parser("privacy")
    pv.add_argument("--staged", action="store_true", help="scan only lines added in the staged diff (pre-commit)")
    pv.add_argument("--history", action="store_true", help="also check every commit's author, committer and message")
    sub.add_parser("todos")
    pb = sub.add_parser("pr-body")
    pb.add_argument("--body-file", required=True, help="file holding the PR description ('-' for stdin)")
    pb.add_argument("--base", help="base ref, to spot dependency changes (e.g. origin/main)")
    args = p.parse_args(argv)

    if args.gate == "privacy":
        problems = gate_privacy(args.staged, args.history)
    elif args.gate == "todos":
        problems = gate_todos()
    else:
        text = sys.stdin.read() if args.body_file == "-" else pathlib.Path(args.body_file).read_text(encoding="utf-8")
        problems = gate_pr_body(text, args.base)

    for line in problems:
        print(f"{args.gate}: {line}", file=sys.stderr)
    if problems:
        return 1
    extra = "" if args.gate != "privacy" or PII_FILE.is_file() or os.environ.get("CI") else \
        " (no .pii-patterns.local, so only emails were checked; see AGENTS.md)"
    print(f"{args.gate}: OK{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
