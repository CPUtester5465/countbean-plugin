---
description: Initialise Countbean — connect to your hosted book (approving in the browser) or create a local one, plus a starter chart of accounts.
argument-hint: "[book name] [currency] | cbk_… bok_…"
---

You are setting up the user's Countbean book. Arguments: `$ARGUMENTS`

Arguments are interpreted by shape, not by position:
- a `cbk_…` token → hosted credentials pasted by hand; treat as `/countbean:connect`.
- otherwise → first token = book name (optional), second = 3-letter currency (optional, default USD).

Do this in order:

1. **Find out where you are.** Call `connection_status`.
   - If the tools are unavailable entirely (no `mcp__countbean__*`), the MCP server did not
     start. Tell the user to run `/mcp` to see the error the server printed — it names the
     cause — and that `/reload-plugins` retries it. First launch builds a Python venv and can
     take ~30s. Stop here; nothing below can work.
   - `HOSTED mode` and reachable → they already have a cloud book. Report it, then go to step 4.
   - `HOSTED mode` but NOT reachable → relay the reason; a revoked or mistyped key is the usual
     one. Offer to reconnect (step 2). Stop.
   - `LOCAL mode` → step 2.

2. **Ask which they want. Do not assume.**

   **Hosted (recommended, free to try)** — their ledger runs on our infrastructure, opens in a
   browser, and is shared with an accountant by invite. Connecting it is TWO tool calls, in
   this order:

   a. `start_device_authorization` — returns immediately with a code and a link.
   b. **Show the user both, verbatim, and wait for them to act.** They open the link, pick which
      book to connect, and approve.
   c. `await_device_approval` — blocks until they approve, then saves the connection.

   Do not call (c) before showing the code from (a). The code is what the user types; if it is
   still sitting in your context when you start blocking, they have nothing to type and the
   grant expires after ten minutes.

   Prefer this over asking for a key. The user never has to find, copy or paste a credential.

   Fall back to `/countbean:connect cbk_… bok_…` only if they already have a key in hand, or
   the browser is on a different machine.

   **Local** — a git-backed Beancount ledger in `~/.countbean/main` on this machine only. No
   browser UI, no sharing, no backups but their own. If they choose this, call `create_book`
   with `name` (from `$1`, else ask, else "My Books") and `currency` (`$2` uppercased, else
   "USD").

3. **Never silently pick one.** A local book the user believes is hosted is the worst outcome
   here: it looks like it works, and none of it is backed up or reachable from the web app.

4. **Seed the opening structure.** Offer to set up a starter chart of accounts. If the user
   agrees (or says something like "set up my money situation"), use `open_accounts` to open
   sensible accounts — typically `Assets:Checking`, `Assets:Savings`,
   `Liabilities:CreditCard`, `Income:*`, `Expenses:*` — following the double-entry rules in the
   countbean-accounting skill. Skip this if the book already has accounts.

5. **Confirm against the book, not your own memory.** Call `book_status` and show the location,
   currency and account count it returns. Then point at what is next:
   `/countbean:ingest <file>` to import statements, `/countbean:report` for reports, and — if
   hosted — the book's page in the browser for the full ledger UI.

If the user has no book yet on the hosted side, `start_device_authorization` will still work but
the approval page will tell them to create one first from their dashboard. Relay that; do not try
to create a hosted book from here — the plugin cannot, and `create_book` is refused in hosted
mode for exactly that reason.

Keep it brief and friendly. Never invent balances — only record what the user tells you.
