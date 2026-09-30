<!--
  Fill in every section. The "PR body" check (scripts/gates.py pr-body) fails on a missing section or one that
  still holds only the italic placeholder. Fix it by editing this description; the check re-runs by itself.
  Never write the maintainer's real name here; use [User].
-->

## What

_The overall effect of this PR, briefly but explicitly._

## Why

_The problem it solves or the goal it serves. Link the issue if there is one._

## How

_Where the core changes are and where to start reviewing. Call out design decisions and the alternatives you rejected._

## Testing

- [ ] `./scripts/check.ps1` passes locally (the full gate set CI runs)
- [ ] _Tests added or changed, and what they prove_
- [ ] _Rendering changes: the PDFs and page images in the `test-artifacts` CI artifact were looked at_

## Task record

_Link the `docs/tasks/TASK_<NAME>.md` for this change, or "None" with the reason (a trivial change)._

## New dependency

None

<!--
  Required whenever the PR changes requirements-dev.txt or REQUIREMENTS in md2pdf.py (the gate checks the diff).
  Replace "None" with: the package and version range, why it is needed, the alternatives considered, and who maintains it.
-->

## Manual intervention

None

<!--
  Does the maintainer have to do anything by hand for this change to work (repository settings, secrets, a release,
  branch protection)? If so, open an issue first with the exact steps, how to verify them, and what stays broken until
  they are done, then replace "None" with its reference, e.g. #12. A PR description is read once; an issue is tracked.
-->

## Open questions

_Follow-up work, open questions, or risks you see but need advice on. "None" is fine._
