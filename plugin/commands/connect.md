---
description: Connect Claude to your hosted Countbean book — approve it in your browser, or paste a key you already have.
argument-hint: "cbk_… bok_…"
---

Connect this Claude session to the user's hosted Countbean book.

Arguments (may arrive in either order, on one line, or not at all): `$ARGUMENTS`

Do this in order:

1. **If `$ARGUMENTS` contains a `cbk_…` token**, call `connect_book` with it (pass the whole
   argument string as `api_key` — the tool sorts `cbk_…` from `bok_…` itself). Then go to step 4.

2. **If there are no arguments**, call `connection_status` first.
   - Already connected and reachable → say which book, and stop. Do not reconnect.
   - Not connected → **prefer browser approval. It needs no key at all**, so do not ask for
     one. Two calls, in this order:
     a. `start_device_authorization` — returns immediately with a short code and a link.
     b. **Show the user both, verbatim, and wait for them to approve.** They open the link,
        pick which book to connect, and approve it.
     c. `await_device_approval` — blocks until they approve, then saves the connection.

     Never call (c) before showing the code from (a): until the user sees it they have
     nothing to type, and the grant expires in ten minutes.
   - Only if the user would rather paste a key by hand — or their browser is on a different
     machine — tell them where it is, in this exact shape:
     > Open your book at **app.countbean.com** → **Connect Claude** → **Create key**.
     > It shows you one line to paste back here. The key is shown once.
     Then stop and wait. Do not guess a key, do not offer to create one — the plugin
     cannot mint keys, only the signed-in book page can.

3. **If the user pastes something that is not a key** (e.g. only a `bok_…`), say which half is
   missing and where it is shown. Never write a partial connection.

4. **Report the result verbatim-ish.** `connect_book` verifies against the live book before it
   saves, so its answer is authoritative:
   - Connected → confirm the book id, then call `book_status` and show the account count and
     latest commit, so the user sees their real book rather than a claim about it.
   - Refused → relay the reason. The common ones are a mistyped paste, a key that was revoked,
     and a key issued for a different book than the id given. Do not retry silently.

Notes:
- This takes effect **immediately** — no restart, no environment variables, no `.env` edit.
  Credentials are stored in `~/.countbean/credentials.json`, readable only by the user.
- If the user would rather use a `.env` or shell exports, those still work and take precedence:
  `COUNTBEAN_API_KEY` and `COUNTBEAN_BOOK_ID`. Mention this only if they ask.
- To undo: `disconnect_book`.
