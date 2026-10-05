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
   `propose_transactions(content=<base64>, account="Assets:Checking", filename="<name>")`, naming
   the account the statement belongs to. If you were given a `statement_id` (`stm_…`), pass that
   instead of `content` — the file is already stored on the book. It parses on the book and
   returns a SUMMARY with a `proposal_id`: counts, date range, totals, the column mapping, what it
   flagged, and each distinct payee once. It never returns the rows and never writes.

2. **Read `mapping` before you read the numbers.** It says which column was taken as the date,
   the description and the amount; which of the three amount shapes it found (a signed column,
   separate debit/credit columns, or a running balance); which date format and decimal separator;
   and whether a positive number was read as money in or money out, *with the evidence* —
   `balance_check` says whether the running balance confirmed it and how many rows it
   contradicted. If any of it is wrong, call again with the override (`amount_shape`, `columns`,
   `date_format`, `delimiter`, `decimal_separator`, `sign`, `opening_balance`) — never by editing
   the amounts.

3. **Categorise by payee.** This is your job and the tool deliberately does not do it. Choose an
   account for each entry in `payees`, following the **countbean-accounting** skill. Payees you
   leave out stay on `Expenses:Unclassified` / `Income:Unclassified`. Flagged rows (`!`) are rows
   the file itself could not settle; say so rather than guessing.

4. **Preview, then commit.** Show the user `counts`, the date range and anything flagged. Once
   they agree, call `commit_proposal(proposal_id, overrides='{"payees": {"<payee>":
   "<account>"}}')`. It writes the parsed rows itself — do **not** re-type them into
   `add_transactions`. The server validates with bean-check and commits to git as one commit; rows
   already in the book are skipped. If it names accounts that are not open, confirm them with the
   user and call again with `open_new_accounts=true`. If it is refused, read the error, fix the
   overrides and retry — never leave the user thinking it saved when it didn't.

5. **Report back** the commit id, how many landed, how many were skipped as already imported, how
   many are still unclassified, and anything flagged. Suggest `/countbean:report` to see the
   updated numbers.

## Everything else

- **PDF statement, or a QIF / JSON export:** there is no importer for these yet. Extract the
  transactions (date, payee, amount, and whether each line is a debit or credit), open any missing
  accounts with `open_accounts`, show the user a preview and write them with `add_transactions`.
- **Plain-text description:** build the transactions directly from what the user said, preview
  them, and write them with `add_transactions`.
- In both cases call `list_accounts` first, map to existing accounts, make every transaction
  balance, and flag anything ambiguous with `!` rather than guessing.

Rules: never fabricate transactions that aren't in the source; keep the user's real payees and
dates; when unsure how to categorise, ask or flag rather than guess.
