---
description: Ingest financial data (bank/credit-card statements, CSV exports, receipts, or a plain-text description) into the book as validated double-entry transactions.
argument-hint: "[path to statement/CSV/receipt | free text]"
---

Ingest the data referenced by `$ARGUMENTS` into the user's Countbean book.

`$ARGUMENTS` may be a file path (CSV, OFX/QFX, PDF, an image of a receipt), a directory of such
files, or a plain-language description ("I got paid $4000, spent $200 on software"). If it's empty,
ask the user what to import.

## CSV, OFX and QFX go through the importer, not through you

`propose_transactions` parses statements. Do **not** read a CSV yourself and work out which column
is the amount: that answer changes between runs, and the tool's does not.

A RECEIPT IS NOT A STATEMENT and takes the other path. Call `stage_receipt` with the path first —
a receipt is the source document for the entry, and it is kept as evidence outside the book. Read
the image yourself (the tool does no OCR, deliberately), then call `propose_receipt_transaction`
with what you read and an honest confidence for each field. It returns a proposal with anything
uncertain flagged rather than filled in, and it will tell you BEFORE the write if the account is
pinned to another currency or was never opened. Show the proposal to the user, ask about anything
flagged, and only then pass its `beancount` to `add_transactions` unchanged.

A PDF bank STATEMENT is neither: extract the transactions from it (date, payee, amount, and
whether each line is a debit or credit).

1. **Hand the bytes to the tool.** Base64 the file and call
   `propose_transactions(content=<base64>, account="Assets:Checking")`, naming the account the
   statement belongs to. It returns proposals, never a write.

2. **Read `mapping` before you read the numbers.** It says which column was taken as the date,
   the description and the amount; which of the three amount shapes it found (a signed column,
   separate debit/credit columns, or a running balance); which date format and decimal separator;
   and whether a positive number was read as money in or money out, *with the evidence*. If any of
   it is wrong, call again with the override (`amount_shape`, `columns`, `date_format`,
   `delimiter`, `decimal_separator`, `sign`, `opening_balance`) — never by editing the amounts.

3. **Open what it asks for.** `accounts_to_open` and `open_directives` are ready to pass to
   `open_accounts`.

4. **Categorise.** This is your job and the tool deliberately does not do it. Every proposal's
   counter-account is a placeholder (`Expenses:Unclassified` / `Income:Unclassified`); replace it
   with a real account, following the **countbean-accounting** skill. Keep the `import-id:`
   metadata line exactly as given — it is what stops the same statement being booked twice — and
   keep the `!` flags, which mark rows the file itself could not settle.

5. **Preview, then commit.** Show the user `counts`, anything flagged, and a short table. Then call
   `add_transactions` with the edited block. The server validates with bean-check and commits to
   git; if it returns `REJECTED`, read the error, fix the entries and retry — never leave the user
   thinking it saved when it didn't.

6. **Report back** the commit id, how many landed, how many were skipped as already imported, and
   anything you flagged. Suggest `/countbean:report` to see the updated numbers.

## Everything else

- **PDF / image receipt:** extract the transactions from it (date, payee, amount, and — for
  statements — whether each line is a debit or credit), then follow steps 3–6. There is no
  importer tool for these yet.
- **Plain-text description:** build the transactions directly from what the user said, then follow
  steps 3–6.
- In both cases call `list_accounts` first, map to existing accounts, make every transaction
  balance, and flag anything ambiguous with `!` rather than guessing.

Rules: never fabricate transactions that aren't in the source; keep the user's real payees and
dates; when unsure how to categorise, ask or flag rather than guess.
