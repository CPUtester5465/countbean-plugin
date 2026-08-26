---
description: Generate a financial report from the book — a styled HTML page and/or an Excel workbook (balance sheet, income statement, transactions).
argument-hint: "[html|excel|both] [as-of date YYYY-MM-DD]"
---

Generate a report from the user's Countbean book. Arguments: `$ARGUMENTS`
(first token = format: `html`, `excel`/`xlsx`, or `both` — default `both`; second token = optional
as-of date `YYYY-MM-DD`, default today).

1. Confirm the book has data by calling `book_status`. If it's empty, tell the user to
   `/countbean:ingest` some data first, and stop.

2. Generate the requested format(s) by calling `generate_report`:
   - HTML → `generate_report(format="html", as_of=<date or "">)`
   - Excel → `generate_report(format="xlsx", as_of=<date or "">)`
   - `both` → call it twice.

3. Each call returns a file path (under the book's `reports/` folder). Report the exact path(s) to
   the user.
   - For the **HTML** report, offer to open it in the browser, and if the user is on a desktop,
     you may run `!`open "<path>" 2>/dev/null || xdg-open "<path>" 2>/dev/null`` to open it.
   - For the **Excel** report, tell them it has three sheets: Balance Sheet, Income Statement,
     and Transactions.

4. Give a short written summary of the headline numbers (net worth, net income for the period)
   drawn from `book_status` so they get the gist without opening the file.

Keep it concise. Do not recompute the numbers yourself — the server's report is the source of truth.
