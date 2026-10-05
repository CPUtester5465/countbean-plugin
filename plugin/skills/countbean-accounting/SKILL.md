---
name: countbean-accounting
description: Rules for writing correct double-entry Beancount transactions when managing a Countbean cloud book — account naming, how to balance postings, categorisation, and safe ingestion. Use whenever recording, importing, or correcting financial data in a Countbean book.
---

# Writing correct books in a Countbean book

A Countbean book is a **Beancount** ledger. Every write goes through the MCP server, which runs
`bean-check` and only commits valid, balancing entries. Your job is to produce correct Beancount
syntax so writes are accepted the first time.

## The five account types

Every account starts with one of these roots. Sign convention matters:

| Root | Normal balance | Goes up when… |
|------|----------------|---------------|
| `Assets` | positive (debit) | money comes in (cash, bank, receivables) |
| `Liabilities` | negative (credit) | you owe more (credit card, loans) |
| `Equity` | negative (credit) | opening balances, retained earnings |
| `Income` | negative (credit) | you earn (income is recorded as a **negative** number) |
| `Expenses` | positive (debit) | you spend |

Accounts are `Colon:Separated:Title-Case`, each segment starting with a capital letter or digit,
e.g. `Assets:Checking`, `Expenses:Food:Groceries`, `Income:Consulting`, `Liabilities:Visa`.

## The one rule: postings must sum to zero

Each transaction has ≥2 postings that net to zero. You can leave **one** posting's amount blank and
Beancount infers it.

```beancount
2026-08-01 * "Blue Bottle" "Oat latte"
  Expenses:Coffee      6.50 USD
  Assets:Checking     -6.50 USD
```

Income example — note the negative on the Income leg:

```beancount
2026-08-01 * "Stripe" "Invoice #204"
  Assets:Checking    1800.00 USD
  Income:Consulting              ; inferred as -1800.00 USD
```

Paying a credit card (transfer between two of your accounts, no income/expense):

```beancount
2026-08-02 * "Payment — Visa"
  Liabilities:Visa     300.00 USD
  Assets:Checking     -300.00 USD
```

## Flags

- `*` = cleared/confirmed. Use for anything you're sure about.
- `!` = pending/needs review. Use when a category is a guess or a match is uncertain — this surfaces
  it in Fava and in reports instead of silently guessing wrong.

## Opening accounts

Open an account **before** it's first used (the server routes `open` directives automatically):

```beancount
2026-01-01 open Assets:Checking   USD
2026-01-01 open Expenses:Software  USD
```

Set a starting balance with a `pad` + `balance` against Equity, or an explicit opening transaction
against `Equity:Opening-Balances`:

```beancount
2026-01-01 * "Opening balance"
  Assets:Checking            9417.30 USD
  Equity:Opening-Balances
```

## Importing bank / card statements

**A CSV, OFX or QFX goes through `propose_transactions`, not through you.** Base64 the bytes (or
pass the `statement_id` you were given), name the account, and it returns a summary: the column
mapping it detected — which column was the amount, which date format, and which way a positive
number points, checked against the running balance when there is one — plus counts, what it
flagged, and each distinct payee once, under a `proposal_id`. It never writes. Choose an account
per payee and write it with `commit_proposal`; never re-type the rows into `add_transactions`.
Reading the file yourself gets a different answer on a different day; the tool does not. Only a
PDF, a QIF, a receipt photo or a spoken description needs you to extract the rows by hand.

Either way:

1. Determine the **account being imported** (e.g. `Assets:Checking` for a bank export,
   `Liabilities:Visa` for a card). Every row has that account as one leg. `propose_transactions`
   requires it as an argument for the same reason: nothing in the file says which account it is.
2. For each row, the **other leg** is a category:
   - money out of checking → an `Expenses:*` account
   - money into checking → an `Income:*` account (or a transfer)
   - a card purchase increases `Liabilities:Visa` (positive) and hits `Expenses:*`
3. Respect the statement's sign/debit-credit columns — don't flip signs.
4. Reuse existing accounts (`list_accounts`); only open new ones when genuinely needed. Keep the
   category tree shallow and consistent (`Expenses:Food`, not five near-duplicates).
5. Flag with `!` anything you can't confidently categorise; never fabricate rows not in the source.
6. When you write rows by hand, keep an `import-id:` metadata line on each if you have one.
   `commit_proposal` writes it for you — it is what makes re-importing the same statement book
   it once instead of twice, and stripping it is silent: nothing fails until the customer has two
   of everything.

## Multi-currency

Amounts always carry a currency: `12.00 USD`, `9.50 EUR`. For a foreign purchase paid from a USD
account, record the price:

```beancount
2026-08-03 * "Hotel Berlin"
  Expenses:Travel     100.00 EUR @ 1.08 USD
  Assets:Checking    -108.00 USD
```

**Currencies with no minor unit — VND, JPY, KRW, IDR.** Their amounts are whole numbers, so a
converted amount almost never lands exactly. Write the rate **exactly as the source gives it**
(every digit on the bank line or invoice), and the account amount the bank actually posted:

```beancount
2026-08-04 * "Acme" "Invoice 204"
  Assets:Bank:VCB     85963610 VND
  Income:Sales        -3396.63 USD @ 25308.5 VND
```

3396.63 × 25308.5 is 85,963,610.355, so the book adds `Expenses:Rounding  0.355 VND` to the
entry itself and says so in the commit message — tell the user, don't redo it. When the source
gives a total rather than a rate, use `@@` with that total instead (`3.00 GBP @@ 100000 VND`).

**Never round the rate yourself.** A rate cut to two decimals (`@ 32247.33` for 32247.3779)
misses by tens of dong, which is a wrong number rather than a rounding, and the write is refused
with "does not balance". Go back to the source for the exact rate, or use `@@` with the total.

## Working with the book (MCP tools)

- `list_accounts`, `get_ledger`, `balances`, `run_query` — read before you write.
- `open_accounts`, `add_transactions`, `add_directives`, `commit_proposal` — writes; each validates + commits.
- If a write returns `REJECTED`, read the bean-check error, fix the entry (usually an unbalanced
  posting or an unopened account), and retry.
- `run_query` uses BQL, e.g. `SELECT account, sum(position) WHERE account ~ 'Expenses:Food'
  GROUP BY account`.

Keep every entry something the user would recognise on their statement. When in doubt, ask or flag —
never guess at real money.
