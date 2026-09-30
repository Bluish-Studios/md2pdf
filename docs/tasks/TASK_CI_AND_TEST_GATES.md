# TASK: CI gates, tests and agent scaffolding

## Original ask

> can you make a basic CI YAML validation pipeline and add unit tests? let's add some gates we can use as
> deterministic checks so we feel good about us or agents doing edits to this project
>
> Unit tests and at least a couple of end to end tests so we know components and e2e are working and changes are safe
>
> I normally aim for 90% code coverage and SOME e2e automation ideally with screenshots or visual artifacts (in this
> case a PDF output is representative enough)

Follow-up, during the same session:

> hey as part of this can you also go through this
> https://github.com/Bluish-Studios/repvault/blob/main/.github/copilot-instructions.md and port over the relevant
> non-repvault parts to this repo?
>
> Things like leaving task documentation, adding test coverage, pre-commit vs full checks, keeping my name out of the
> repo. Don't worry about the database and service stuff - this repo is a simple CLI tool. But maybe I will add at
> least a security gate. Oh and can you add a PR template based on the RepVault template too?
>
> Trying to keep this lightweight but build enough scaffolding that we can trust agents to do work, leave a record of
> their work, and run trusted CI gates that show clear deterministic pass/fail before any merge

## Approach

One script, `scripts/check.ps1`, runs every gate. The git hooks call it (`-Mode fast` before a commit, the full set
before a push) and so does CI, so a local pass predicts a CI pass and there's only one list of gates to maintain.
CI adds what needs the network or full history (gitleaks, pip-audit) and the PR-description check.

Tests work in three layers:

- **Unit** (`tests/test_*.py`): every function in `md2pdf.py`, with fakes for Edge (a fake DevTools WebSocket and
  fake processes), pip, the registry and the network.
- **End-to-end** (`tests/e2e/test_convert.py`): real conversions in headless Edge, run in-process so they count
  toward coverage. They check page sizes, layout choice, that contents-page numbers match where headings really are,
  that chapters start at the top of a page, link and bookmark counts, mermaid, sanitizing and warnings. Every PDF is
  kept with a PNG per page and a contact sheet in `test-artifacts/`, which CI uploads.
- **Launcher** (`tests/e2e/test_launcher.py`): `md2pdf.cmd` and `md2pdf.ps1` from a fresh copy. This covers the real
  first-run pip install, a conversion, and exit codes passing through cmd → PowerShell → Python.

## Root causes found by the new tests

1. **Edge exits at once when the host sets `__COMPAT_LAYER`.** Running `python md2pdf.py` from a host that sets the
   Windows app-compatibility variable `__COMPAT_LAYER` (seen as `DetectorsAppHealth` in an agent session) made Edge
   exit with code 0 before opening its DevTools port: "could not start headless Edge". The launchers weren't
   affected, because Windows PowerShell 5.1 drops the variable. Fixed in `launch_edge`, which now starts Edge without
   it (unit test in `test_setup.py`).
2. **Doubled PDF bookmark titles** (not fixed, #3). A heading that starts a page gets its title twice in Edge's PDF
   outline, e.g. `ContentsContents`. Recorded as a strict xfail: `test_bookmark_titles_are_not_doubled`.
3. **Two mermaid diagrams collide** (not fixed, #4). Every diagram's SVG gets the id `mermaid-0`, so the second
   diagram's styles and markers clash with the first: in the showcase contact sheet the two diagrams draw on top of
   each other. Recorded as a strict xfail: `test_mermaid_diagrams_get_unique_svg_ids`.

## Completed

| Change | Files |
|---|---|
| Unit tests for text, HTML sanitizing, Doc, rendering, CLI, setup, Edge launch, the DevTools client, PDF readers | `tests/test_text.py`, `test_doc.py`, `test_cli.py`, `test_setup.py`, `test_cdp.py`, `tests/conftest.py` |
| End-to-end and launcher tests, with PDF, page-image and contact-sheet artifacts | `tests/e2e/*`, `tests/visual.py`, `tests/fixtures/*` |
| Gate runner and git hooks | `scripts/check.ps1`, `.githooks/pre-commit`, `.githooks/pre-push` |
| Privacy, TODO-link and PR-description gates, with tests | `scripts/gates.py`, `tests/test_gates.py` |
| Lint and security lint (ruff with bandit rules), coverage floor, pytest markers | `pyproject.toml`, `PSScriptAnalyzerSettings.psd1` |
| CI: lint, tests on Python 3.10 and 3.13 with Edge, security (gitleaks, pip-audit), PR body | `.github/workflows/ci.yml`, `.github/workflows/pr-body.yml` |
| Dependabot for actions and pip | `.github/dependabot.yml` |
| Agent instructions, task-record format, PR template | `AGENTS.md`, `CLAUDE.md`, `docs/tasks/README.md`, `.github/pull_request_template.md` |
| `__COMPAT_LAYER` fix; `# noqa` reasons on accepted security-lint findings | `md2pdf.py` |
| Dev setup section; ignore rules for tools and test output | `README.md`, `.gitignore` |

## Key decisions

- **One gate script for hooks and CI** instead of separate YAML steps. Rejected: pre-commit (the framework), because
  it adds a dependency and its own configuration language for what one PowerShell script does, and the launcher is
  Windows-only anyway.
- **All test jobs on `windows-latest`**, because it has Edge preinstalled and the launchers are Windows-only.
  Rejected: Linux runners for unit tests, which would need platform branches in the tests for little saving.
- **In-process e2e** (`md2pdf.main()`), so real Edge runs count toward coverage. Only the launcher tests use
  subprocesses. They also run the real first-run pip install, which the in-process tests skip.
- **No pixel-diff visual regression.** Font rendering and Edge updates would make it flaky, and a flaky gate gets
  ignored. Instead the tests make deterministic checks (page sizes, text on each page, contents numbers against
  bookmark pages, link counts, no blank pages) and keep the images for a person to review.
- **AGENTS.md, not `.github/copilot-instructions.md`**: one file that Copilot, Codex and Claude Code (via
  `CLAUDE.md`) all read. Ported from RepVault: task records, two-stage checks, the `[User]` rule with a local-only
  pattern file, security invariants, the gates table, manual-intervention issues, strict xfail for known bugs.
  Left out: database, service, auth, frontend, deploy and scenario rules, status-claim and skills-sync gates.
- **Security gate = gitleaks (full history, pinned binary with checksum) + pip-audit + ruff `S` rules.** Rejected:
  bandit as a separate tool, since ruff's `S` rules cover the same checks with no extra dependency.
- **Known bugs as `xfail(strict=True)`**, so the suite stays green today and turns red when the bug is fixed without
  anyone updating the test.

## Remaining

- Fix the doubled bookmark titles (#3) and the colliding mermaid diagrams (#4), then remove their xfail marks.
- Check the first hosted CI run of each job, especially the Python 3.10 matrix leg and the PSScriptAnalyzer step,
  which ran only locally on PowerShell 7.

## Manual intervention

#2: require the CI checks before merging to `main`. Branch protection is a repository setting only the maintainer
can change, and GitHub offers it for private repositories only on paid plans, so it can be turned on once the
repository is public.
