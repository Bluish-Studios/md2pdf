# Task records

A task record is the write-up of a change worth finding again in six months: a fix with a root cause, a design
decision, new tooling or gates, a refactor. It outlives the session and the PR. Commit messages say *what*; the task
record says *why*, and which options were rejected.

- **Trivial change** (a typo, a one-line fix with an obvious cause): a good commit message is enough.
- **Everything else**: write `docs/tasks/TASK_<NAME>.md`. Name it after the intent, not the mechanics:
  `TASK_CI_AND_TEST_GATES.md`, not `TASK_EDIT_YAML.md`.

## What it must contain

1. **The original ask**, verbatim. Paraphrasing loses what you'll want later. Replace the maintainer's name with `[User]`.
2. **Approach / root cause**: for a bug, what was actually wrong, not just the symptom.
3. **Completed**: a table of change → files.
4. **Key decisions and rationale**, including the options you considered and rejected. This is the part that pays
   off later: a decision without its reasoning gets re-argued.
5. **Remaining**: exact next steps with issue links, or "None". Be honest about what was skipped or couldn't be verified.
6. **Manual intervention**: anything the maintainer must do by hand, with its tracking issue, or "None".

## What it must not contain

- **Test counts, coverage percentages or file counts.** They go stale at once. Write "the suite passes" and let the
  reader run it.
- **The maintainer's real name, emails or local paths.** Use `[User]` and repository-relative paths.
- **Secrets or document contents**, even in examples.

## Keep it current

Write the record once the shape of the work is clear, and update it at milestones and before the PR is ready.
Link it from the PR's `## Task record` section.
