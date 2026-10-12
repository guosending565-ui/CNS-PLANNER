---
name: cns-git-closeout
description: Close out an authorized CNS-PLANNER change using the fixed status, diff, targeted-tests, commit, and push sequence. Use when the user explicitly asks to commit/push or finish a branch; never for merges or history rewriting.
---

# CNS Git Closeout

Execute this order without skipping ahead:

1. **Status** — run `git status --short --branch`; confirm the current branch, upstream, and which changes belong to the requested task.
2. **Diff** — inspect `git diff --stat`, the scoped diff, and staged diff if present. Preserve unrelated user changes and generated/untracked output.
3. **Targeted tests** — run the smallest relevant tests from `README.md` §16 plus `git diff --check`. Do not run full pytest unless explicitly requested.
4. **Commit** — only after tests pass and only when the user authorized a commit. Stage explicit task files, review `git diff --cached`, then create one focused commit with a concise message.
5. **Push** — only when the user authorized a push. Push the current branch to its configured upstream, or establish the same-name upstream if needed.

## Hard stops

- Never merge, rebase, or pull `main` as part of closeout.
- Never force push (`--force`, `--force-with-lease`) or rewrite history.
- Never discard/reset unrelated changes, stage with `git add -A`, or commit generated diagnostics/artifacts by convenience.
- If tests fail, the branch/upstream is ambiguous, or the scoped files overlap unrelated work, stop before commit/push and report the exact blocker.
- Commit and push are separate authorization-sensitive steps; a request to inspect or test does not authorize either.

Finish by reporting branch, commit hash (if created), pushed ref (if pushed), tests run, and remaining worktree changes.
