---
description: Show the current state of your Countbean book — balances, net worth, account count, and recent changes.
---

Call the `book_status` tool and present the result to the user clearly: book name and location,
operating currency, number of accounts, net worth (assets + liabilities), net income, and the
list of recent commits. If no book exists yet, tell them to run `/countbean:init`.

If the user asked a specific question in `$ARGUMENTS` (e.g. "how much did I spend on food?"),
answer it by calling `run_query` or `balances` with an appropriate BQL filter rather than only
showing the summary.
