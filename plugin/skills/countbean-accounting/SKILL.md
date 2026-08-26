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

**A CSV, OFX or QFX goes through `propose_transactions`, not through you.** Base64 the bytes,
name the account, and it returns proposed transactions plus the column mapping it detected —
which column was the amount, which date format, and which way a positive number points. It never
writes. Reading the file yourself gets a different answer on a different day; the tool does not.
Only a PDF, a receipt photo or a spoken description needs you to extract the rows by hand.

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
6. Keep the `import-id:` metadata line `propose_transactions` puts on every proposal. It is what
   makes re-importing the same statement book it once instead of twice, and stripping it is
   silent — nothing fails until the customer has two of everything.

## Multi-currency

Amounts always carry a currency: `12.00 USD`, `9.50 EUR`. For a foreign purchase paid from a USD
account, record the price:

```beancount
2026-08-03 * "Hotel Berlin"
  Expenses:Travel     100.00 EUR @ 1.08 USD
  Assets:Checking    -108.00 USD
```

## Working with the book (MCP tools)

- `list_accounts`, `get_ledger`, `balances`, `run_query` — read before you write.
- `open_accounts`, `add_transactions`, `add_directives` — writes; each validates + commits.
- If a write returns `REJECTED`, read the bean-check error, fix the entry (usually an unbalanced
  posting or an unopened account), and retry.
- `run_query` uses BQL, e.g. `SELECT account, sum(position) WHERE account ~ 'Expenses:Food'
  GROUP BY account`.

Keep every entry something the user would recognise on their statement. When in doubt, ask or flag —
never guess at real money.
