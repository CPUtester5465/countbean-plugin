---
description: Undo a change to your book by reverting a commit from its git history.
argument-hint: "[commit id]"
---

Help the user undo a change. Arguments: `$ARGUMENTS` (optional commit id).

1. Call `history` to show recent commits.
2. If `$1` is a commit id, call `revert(commit="$1")`.
   Otherwise show the history and ask which change to undo.
3. Confirm the new state with `book_status`.

Because every change is a git commit, reverting is always safe and itself recorded — nothing is
lost, it's just a new commit that backs out the old one.
