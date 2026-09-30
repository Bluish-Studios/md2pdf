# Agent instructions — md2pdf

md2pdf converts Markdown to paginated PDFs laid out for reMarkable tablets or paper. It's a single Python script
(`md2pdf.py`) plus two Windows launchers (`md2pdf.ps1`, `md2pdf.cmd`). Pages are printed by headless Microsoft Edge on
the local machine. `README.md` is the user guide. This file tells people and coding agents how to change the project
safely. Claude Code reads it through `CLAUDE.md`; Copilot, Codex and others read it directly.

## Layout

| Path | What |
|---|---|
| `md2pdf.py` | the whole engine: Markdown → HTML → Edge (DevTools protocol) → PDF |
| `md2pdf.ps1`, `md2pdf.cmd` | launchers: find or install Python, then run `md2pdf.py` |
| `tests/` | unit tests; `tests/e2e/` drives real Edge and the launchers; `tests/fixtures/` holds sample documents |
| `scripts/check.ps1` | runs every gate; the git hooks and CI call it |
| `scripts/gates.py` | the privacy, TODO-link and PR-description gates |
| `docs/tasks/` | task records: the durable write-up of each non-trivial change |

## Commands

| Command | What it does |
|---|---|
| `./scripts/check.ps1 -Mode setup` | once: create `.venv`, install `requirements-dev.txt`, turn on the git hooks |
| `./scripts/check.ps1 -Mode fast` | the pre-commit gates, in seconds: lint, privacy, TODO links, unit tests |
| `./scripts/check.ps1` | the full gate set (pre-push and CI): adds PSScriptAnalyzer, end-to-end tests with Edge, launcher tests and the coverage floor |
| `.venv/Scripts/python -m pytest -m "not e2e"` | unit tests only, for a tight loop |
| `.venv/Scripts/python md2pdf.py tests/fixtures/showcase.md -o out.pdf` | try a change by hand |

Don't trust test counts or coverage numbers written anywhere, this file included. Run the suite.

## Three rules for every session

### 1. Leave a task record for any non-trivial change

A change that touches several files, a bug fix with a root cause worth remembering, or a design decision gets a
write-up in `docs/tasks/TASK_<NAME>.md`: the original ask verbatim, the root cause, what changed, the decisions and
the options you rejected, and what's left. Format and rules are in [`docs/tasks/README.md`](docs/tasks/README.md).
Link it from the PR's `## Task record` section. A typo fix doesn't need one.

### 2. Fast checks on commit, the full check before push

The pre-commit hook runs `check.ps1 -Mode fast` (seconds). The pre-push hook runs the full `check.ps1`, which is the
same gate set CI runs, so a local pass predicts a CI pass. The full check is skipped only on `wip/*` and `tmp/*`
branches or with `SKIP_PREFLIGHT=1`. **Never use `--no-verify` for a real commit or push.** If a gate is wrong, fix the gate
in its own change, with a test.

### 3. Never write the maintainer's real name into the repo

Use the placeholder **`[User]`** in any doc, comment, commit message, PR description, fixture or test you add or
change. In prose, say "the maintainer". This holds even though the project has a single maintainer. Commits must
use a GitHub noreply address (`<id>+<login>@users.noreply.github.com`); the privacy gate checks every commit's author,
committer and message, and every tracked file, for any other email. To block the name itself locally, create the
**untracked** file `.pii-patterns.local` (one case-insensitive regex per line; it's gitignored so the name never
lands in the repo). The pre-commit gate then checks staged lines against it. Don't put the real name into this file,
an example or a test.

## Security invariants

md2pdf is used on sensitive documents. These are product guarantees, not preferences:

1. **Documents never leave the machine.** Edge runs headless with a throwaway profile and background networking
   turned off. The only network use is PyPI (first run), python.org (only if Python is missing) and mermaid.js from
   jsDelivr/unpkg (once). Adding any other network call is a maintainer decision; propose it in the PR, don't just add it.
2. **Only Microsoft Edge renders documents.** Never download or run another browser.
3. **HTML from documents is sanitized.** Scripts, forms, frames and event handlers are removed (`sanitize_html`).
   Anything that widens what passes through needs a test that proves a script still can't run.
4. **No secrets anywhere:** not in code, tests, fixtures, docs or logs. gitleaks scans the full history in CI.
5. **Dependencies are a supply-chain decision.** A change to `REQUIREMENTS` in `md2pdf.py` or to `requirements-dev.txt`
   needs a `## New dependency` justification in the PR (the PR-body gate checks the diff). pip-audit blocks known
   vulnerabilities. Keep the `requirements-dev.txt` runtime lines identical to `REQUIREMENTS` (a test checks).

## The gates

Each gate fails with a message that says exactly what's wrong. You don't need to memorize the rules, just know the
gate exists and run `./scripts/check.ps1`.

| Gate | Enforces | Where |
|---|---|---|
| ruff | lint, plus the flake8-bandit security rules (`S`). An accepted finding needs `# noqa: Sxxx - reason` on its line. | fast, CI gates |
| actionlint | workflow YAML is valid and safe | fast, CI gates |
| PowerShell syntax + PSScriptAnalyzer | the launcher and scripts parse and pass analysis (`PSScriptAnalyzerSettings.psd1`) | full, CI gates |
| privacy | no personal emails in files or commits; `.pii-patterns.local` locally | fast (staged), CI gates (history) |
| TODO links | every TODO/FIXME in code links an issue (`#12` or its URL) | fast, CI gates |
| unit tests | behaviour of each function, with fakes for Edge and the network | fast, CI gates |
| e2e + launcher tests | real conversions in Edge; the launchers from a fresh copy, including the first-run install | full, CI gates |
| coverage floor | at least 90% line coverage of `md2pdf.py` over the whole suite (`pyproject.toml`) | full, CI gates |
| gitleaks | no secrets in any commit | CI security |
| pip-audit | no known vulnerabilities in the dependencies a first run installs | CI security |
| PR body | the description follows `.github/pull_request_template.md`; dependency changes are justified | CI pr-body |

Judgment calls the gates can't make for you: **don't lower the coverage floor, add a `noqa`, or loosen a gate
to get a change through.** That's a maintainer decision, so ask. **Don't delete a TODO to satisfy its gate;** file the issue.

## Testing expectations

- New behaviour comes with unit tests next to the matching area (`test_text.py`, `test_doc.py`, `test_cli.py`,
  `test_setup.py`, `test_cdp.py`). Fake Edge and the network in unit tests; the e2e suite covers the real thing.
- A change to how pages look adds or updates an e2e test in `tests/e2e/test_convert.py`, and uses its `keep` fixture
  so the PDF, a PNG of every page and a contact sheet land in `test-artifacts/`. CI uploads that folder, so reviewers
  can look at the pages. Look at the contact sheet yourself before you call a rendering change done.
- A known bug that isn't fixed yet gets a test marked `@pytest.mark.xfail(strict=True, reason=...)`. When someone fixes
  it the test passes, the strict mark fails the run, and the mark gets removed. Don't delete a failing test to get green.
- End-to-end tests are skipped locally when Edge isn't installed. In CI (`CI` is set) a missing Edge fails.

## What agents can and can't do

| Environment | Can do | Can't do |
|---|---|---|
| Local Windows session with Edge | everything in `check.ps1`, including e2e and launcher tests | change the maintainer's git identity, PATH or Edge policies |
| Cloud or Linux session without Edge | edit code, `check.ps1 -Mode lint`, unit tests (e2e skip) | claim rendering works: say it's unverified and let CI or the maintainer check |
| Maintainer only | repository settings, branch protection, secrets, releases, making the repo public | — |

**Manual-intervention protocol:** if the maintainer has to do something by hand (a repository setting, a secret, a
release), open a GitHub issue with the exact steps, how to verify them, and what stays broken until they're done.
Link it in the PR's `## Manual intervention` section. A PR description is read once; an issue is tracked.

## PR descriptions

`gh pr create` and API-created PRs never see the template, so start from `.github/pull_request_template.md` and fill
every section. The `PR body` check re-runs when the description is edited. Fix a failure by editing the description;
"Re-run jobs" replays the old description.
