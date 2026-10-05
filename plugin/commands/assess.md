---
description: Review the book and report what the numbers actually show — run rate, categories, changes, unusual entries, and gaps in the data.
argument-hint: "[what to focus on]"
---

Review the user's book and report findings. Focus, if given: `$ARGUMENTS`

**Call `assess_book` and report what it returns. Do not do your own arithmetic on the ledger.**
The tool computes every figure, and it computes them carefully — which months are complete,
whether there is enough data for a run rate, what counts as unusual for a given account. Those
decisions are the difference between a report and a guess.

## The rules, in order of how much damage breaking them does

1. **`sufficient: false` means you do not have the number.** It is not a number with a caveat.
   Say what is missing and what would fix it — usually "another month or two of recording".
   Never estimate around it, never say "roughly" or "on track for".

2. **Only complete months are averaged.** The tool marks each month `complete`. The current
   month is almost never complete, and including it makes spending look like it fell in every
   category. Report the current month as its own line, clearly labelled as still in progress.

3. **State the window.** Every finding is about `coverage.first_date` to `coverage.last_date`.
   A reader who does not know the window cannot judge anything you say.

4. **An outlier is not an error.** `outliers` are postings large relative to their own account.
   The annual insurance premium lands here every year and is correct. Present them as "worth a
   look", never as mistakes, and never imply fraud.

5. **`posting_count` is postings, not transactions.** One transaction has at least two. Do not
   call it a transaction count.

6. **Do not advise on tax, or tell the user what to do with their money.** Describe what the
   book shows. If they ask what to do about it, answer as a bookkeeper would — what the numbers
   mean — and suggest their accountant for anything that turns on jurisdiction or intent.

## Shape of the report

Lead with the window and one honest sentence about what the data can support. Then:

- **Where the money went** — top categories from `categories`, with shares. Group the long tail.
- **What changed** — `movers` between the two named complete months. `pct_change: null` with
  `note: "new this month"` means a new category, not infinite growth.
- **Run rate** — from `run_rate` if `sufficient`, always with `months_used`. If it carries a
  `runway_months`, quote `runway_note` alongside it: it assumes the next months look like the
  last ones, which is an assumption and should be said out loud.
- **Worth a look** — `outliers`, with the account's own median for context.
- **Gaps** — `data_quality`. These are the findings the user can act on immediately, and
  uncategorised spending makes every category figure above it less trustworthy, so say so.

Keep it short. A table beats a paragraph. If the book is nearly empty, say that in two lines and
suggest `/countbean:ingest` rather than padding a report out of four transactions.
