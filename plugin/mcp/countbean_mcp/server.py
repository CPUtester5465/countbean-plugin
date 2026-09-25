"""Countbean MCP server.

Exposes a user's git-backed Beancount "cloud book" as MCP tools so an AI agent
can read and safely write double-entry books. Every mutating tool validates
with bean-check and commits to git; a rejected change never lands.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .ledger import Ledger, LedgerError
import time

from .hosted import (
    HostedBook,
    HostedConfig,
    HostedNotFoundError,
    HostedUnavailableError,
    config_from_env,
    poll_device_authorization,
    DeviceFlowError,
    start_device_authorization as start_device_authorization_request,
)
from . import assessment, credentials, importing, proposals, receipts, reporting

mcp = FastMCP("countbean")


def _book():
    """The book this plugin is talking to — hosted if configured, else local.

    Hosted is selected by COUNTBEAN_API_KEY and is the path a paying customer
    is on. Local mode is unchanged and still useful (trying the product out, a
    book you keep yourself), but it is no longer the silent default for someone
    who believes they are editing the book we host — see hosted.py.

    Resolved per call rather than at import, so `connect_book` takes effect on
    the very next tool call instead of the next Claude Code launch. That is the
    difference between "paste one line" and "quit, edit your shell, relaunch".
    """
    cfg = config_from_env()
    if cfg is not None:
        return HostedBook(cfg)
    # `.mcp.json` used to hard-set COUNTBEAN_BOOK to this same default, which
    # made it ALWAYS present in the server's environment — so a `.env` could
    # never choose a local book path, because the highest-precedence source was
    # permanently occupied by a default. The default belongs here, after every
    # source has had its say.
    resolved = credentials.resolve()
    root = resolved.get("COUNTBEAN_BOOK") or str(Path.home() / ".countbean" / "main")
    return Ledger(root)


def _is_hosted(book) -> bool:
    return isinstance(book, HostedBook)


def _require(ledger: Ledger) -> Ledger:
    if not ledger.exists():
        raise LedgerError(
            f"No book at {ledger.root}. Create one with `create_book` "
            f"(or the /countbean:init command)."
        )
    return ledger


# -------------------------------------------------------------- connection ---
@mcp.tool()
def connect_book(api_key: str, book_id: str = "", control_url: str = "") -> str:
    """Connect this plugin to a hosted Countbean book, permanently.

    Give it the key shown once on your book's page (`cbk_…`) and the book id
    (`bok_…`). Verifies the pair against the live book BEFORE saving, then
    stores it in ~/.countbean/credentials.json (0600). Takes effect immediately
    — no restart, no environment variables.

    Paste both on one line and this tool sorts them out; the `bok_…` id can be
    omitted if you have already connected to that book before.
    """
    api_key, book_id = _split_pasted(api_key, book_id)
    if not api_key.startswith("cbk_"):
        return (
            "That does not look like a Countbean API key. Keys start with "
            "'cbk_' and are shown once, on your book's page under "
            "'Connect Claude'. Nothing was saved."
        )
    if not book_id:
        book_id = credentials.resolve().get("COUNTBEAN_BOOK_ID", "")
    if not book_id:
        return (
            "I need the book id too — it looks like 'bok_…' and sits next to "
            "the key on your book's page. Nothing was saved."
        )

    # Verify BEFORE writing. A saved credential that does not work is worse
    # than none: every later failure looks like a broken product rather than a
    # mistyped paste, and the customer has no reason to suspect this step.
    cfg = HostedConfig(
        control_url=(control_url or "https://api.countbean.com"),
        api_key=api_key,
        book_id=book_id,
    )
    try:
        HostedBook(cfg).exists()
    except LedgerError as e:
        return f"Did not connect — the book refused those credentials.\n{e}\nNothing was saved."

    path = credentials.save(api_key, book_id, control_url)
    return (
        f"Connected to book {book_id}.\n"
        f"Verified against {cfg.api_base} and saved to {path} (readable only by you).\n"
        f"Every countbean tool now reads and writes THAT book. "
        f"Use `disconnect_book` to undo it."
    )


def _split_pasted(api_key: str, book_id: str) -> tuple[str, str]:
    """Tolerate one pasted blob instead of two tidy arguments.

    The realistic input is a line copied off the book page, and it may arrive
    as "cbk_x bok_y", as two arguments, or in the other order. The prefixes are
    unambiguous, so sort by prefix rather than by position and stop making the
    customer's paste the thing that has to be correct.
    """
    tokens = f"{api_key} {book_id}".replace(",", " ").split()
    key = next((t for t in tokens if t.startswith("cbk_")), "")
    book = next((t for t in tokens if t.startswith("bok_")), "")
    if not key and not book:
        return api_key.strip(), book_id.strip()
    return key, book


@mcp.tool()
def start_device_authorization(control_url: str = "") -> str:
    """Step 1 of connecting a hosted book: get a code for the user to approve.

    Returns IMMEDIATELY with a short code and a link. Show both to the user,
    then call `await_device_approval` to wait for them to approve it.

    Deliberately two tools and not one. An MCP tool returns a single result, at
    the end — so a tool that fetched the code and then waited for approval could
    never show the code to the person who has to type it. It could only ever
    expire. That was the first version of this, and it was unusable.
    """
    base = (control_url or credentials.resolve().get("COUNTBEAN_CONTROL_URL", "https://api.countbean.com")).rstrip("/")
    try:
        grant = start_device_authorization_request(base)
    except DeviceFlowError as e:
        return f"Could not start the approval: {e}"

    credentials.save_pending_device({
        "device_code": grant["device_code"],
        "user_code": grant.get("user_code", ""),
        "verification_uri": grant.get("verification_uri_complete")
        or grant.get("verification_uri", ""),
        "control_url": base,
        "interval": float(grant.get("interval", 5)),
        "expires_in": float(grant.get("expires_in", 600)),
    })
    minutes = int(float(grant.get("expires_in", 600)) // 60)
    return (
        f"Code: {grant.get('user_code','')}\n"
        f"Open: {grant.get('verification_uri_complete') or grant.get('verification_uri','')}\n"
        f"Expires in {minutes} minutes.\n\n"
        "Show the user the code and the link, tell them to pick which book to "
        "connect and approve, then call `await_device_approval`."
    )


@mcp.tool()
def await_device_approval() -> str:
    """Step 2: wait for the user to approve the code from `start_device_authorization`.

    Blocks until they approve, decline, or the code expires. On success the
    connection is saved and every countbean tool switches to that book.
    """
    pending = credentials.load_pending_device()
    if not pending:
        return (
            "Nothing is waiting for approval. Call `start_device_authorization` "
            "first, show the user the code, then call this."
        )
    base = pending.get("control_url", "https://api.countbean.com")
    try:
        result = poll_device_authorization(
            base,
            pending["device_code"],
            float(pending.get("interval", 5)),
            float(pending.get("expires_in", 600)),
        )
    except DeviceFlowError as e:
        credentials.clear_pending_device()
        return f"Not connected.\n{e}"
    finally:
        pass

    api_key = result.get("api_key", "")
    book_id = result.get("book_id", "")
    credentials.clear_pending_device()
    if not api_key or not book_id:
        return "The approval returned nothing usable. Nothing was saved. Try again."

    path = credentials.save(
        api_key, book_id, "" if base == "https://api.countbean.com" else base
    )
    reachable, detail, will_clear = _confirm_reachable(api_key, book_id, base)
    if reachable:
        tail = "Verified: the book answered and confirmed its id."
    elif will_clear:
        tail = (
            f"Saved, but the book has not answered yet ({detail}). A book that "
            "was just provisioned takes a few seconds to start — try a "
            "countbean tool shortly."
        )
    else:
        # The key is good and the connection is saved; the book is refusing for
        # a reason that waiting does not fix (#365 — a paused book is the first
        # one). Repeating the cold-start advice here would send the customer
        # into the retry loop the control-plane's message exists to prevent.
        tail = f"Saved, but the book will not answer yet. {detail}"
    return (
        f"Connected to book {book_id}.\n"
        f"Saved to {path} (readable only by you).\n"
        f"{tail}\n"
        "Every countbean tool now reads and writes THAT book."
    )


def _confirm_reachable(api_key: str, book_id: str, control_url: str):
    """Reachability check that tolerates a cold start.

    A machine provisioned moments ago answers 409 "starting up" through the
    proxy for the first few seconds. Reporting that as a bad credential sends
    somebody to reissue a key that is perfectly good — which is exactly what
    the first version did, seconds after a successful approval.

    Returns ``(reachable, detail, will_clear)``. ``will_clear`` says whether
    waiting is the right advice, and it exists because this function is the
    reason ``HostedPausedError`` is not a ``HostedUnavailableError``: only that
    type is slept on, and only its failures resolve themselves. Every other
    ``LedgerError`` — a bad key, a paused book — is answered now and will be
    answered the same way in thirty seconds.
    """
    book = HostedBook(
        HostedConfig(control_url=control_url, api_key=api_key, book_id=book_id)
    )
    last = ""
    for _ in range(6):
        try:
            book.exists()
            return True, "", True
        except HostedUnavailableError as e:
            last = str(e)
            time.sleep(5)
        except LedgerError as e:
            return False, str(e), False
    return False, last or "still starting", True


@mcp.tool()
def disconnect_book() -> str:
    """Forget the saved hosted-book connection (the key stays valid; revoke it
    on the book's page if you want it dead)."""
    if credentials.forget():
        return (
            "Disconnected. The saved credentials are deleted from this "
            "machine. The API key itself still works — revoke it on your "
            "book's page if you want it to stop."
        )
    return "There was no saved connection to remove."


@mcp.tool()
def connection_status() -> str:
    """Show which book this plugin is talking to, and which config chose it.

    Answers the question that actually gets asked when something looks wrong:
    not "is it configured" but "WHICH of my configs won". Environment beats a
    .env, which beats the saved connection.
    """
    resolved = credentials.resolve()
    key = resolved.get("COUNTBEAN_API_KEY", "")
    if not key:
        root = resolved.get("COUNTBEAN_BOOK") or str(Path.home() / ".countbean" / "main")
        return (
            "LOCAL mode — no hosted book connected.\n"
            f"Reads and writes go to {root} on this machine.\n"
            "To use your hosted Countbean book, open its page, create a "
            "connection key, and paste the line it gives you into Claude "
            "(/countbean:connect)."
        )
    book_id = resolved.get("COUNTBEAN_BOOK_ID", "(missing)")
    lines = [
        "HOSTED mode.",
        f"  Book id     {book_id}   [from {credentials.source_of('COUNTBEAN_BOOK_ID')}]",
        f"  API key     {key[:8]}…      [from {credentials.source_of('COUNTBEAN_API_KEY')}]",
        f"  Control URL {resolved.get('COUNTBEAN_CONTROL_URL', 'https://api.countbean.com')}",
    ]
    try:
        HostedBook(HostedConfig(
            control_url=resolved.get("COUNTBEAN_CONTROL_URL", "https://api.countbean.com"),
            api_key=key,
            book_id=book_id,
            book_url=resolved.get("COUNTBEAN_BOOK_URL", ""),
        )).exists()
        lines.append("  Reachable   yes — the book answered and confirmed its id.")
    except LedgerError as e:
        lines.append(f"  Reachable   NO — {e}")
    return "\n".join(lines)


# --------------------------------------------------------------- lifecycle ---
@mcp.tool()
def create_book(name: str = "My Books", currency: str = "USD") -> str:
    """Initialise a new, empty cloud book (git-backed Beancount ledger).

    Safe to call once per book; errors if a book already exists at the location.
    """
    ledger = _book()
    res = ledger.init(name=name, currency=currency)
    return f"Created book '{name}' ({currency}) at {ledger.root} [commit {res.commit}]"


@mcp.tool()
def book_status() -> str:
    """Summarise the current book: location, account count, balances, last commits."""
    ledger = _book()
    if not ledger.exists():
        return f"No book yet at {ledger.root}. Call create_book to start."
    if _is_hosted(ledger):
        # `reporting.collect` parses files off disk, which a hosted book does
        # not have here. Ask the book itself instead of pulling the whole
        # ledger down to recompute what it already knows.
        rows = ledger.balances()
        hist = ledger.history(5)
        return "\n".join([
            f"Book: {ledger.root}  [HOSTED]",
            f"Accounts with balances: {len(rows)}",
            *[f"  {r.get('account',''):<40} {r.get('balance','')}" for r in rows[:20]],
            "Recent commits:",
            *[f"  {h['commit']} {h['date']} {h['message']}" for h in hist],
        ])
    accounts = ledger.accounts()
    hist = ledger.history(5)
    data = reporting.collect(ledger)
    t = data["totals"]
    lines = [
        f"Book: {data['title']} ({data['currency']}) at {ledger.root}",
        f"Accounts: {len(accounts)}",
        f"Net worth: {t['net_worth']:,.2f} {data['currency']}  "
        f"(assets {t['assets']:,.2f}, liabilities {t['liabilities']:,.2f})",
        # Year to date, not since inception (#638) — the label says which.
        f"Net income {data['period_start']} to {data['as_of']}: "
        f"{t['net_income']:,.2f} {data['currency']}",
        "Recent commits:",
        *[f"  {h['commit']} {h['date']} {h['message']}" for h in hist],
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- writes -----
@mcp.tool()
def add_transactions(beancount_text: str) -> str:
    """Append one or more transactions (raw Beancount syntax) to the book.

    The text is validated with bean-check and only committed if valid; on
    failure nothing is written and the validation errors are returned.
    Postings must balance. Open any new accounts first (open_accounts).
    """
    ledger = _require(_book())
    try:
        res = ledger.add_directives(beancount_text, "Add transactions via AI")
    except LedgerError as e:
        return f"REJECTED — ledger left unchanged.\n{e}"
    return f"Committed {res.commit}: {res.message}"


@mcp.tool()
def open_accounts(beancount_text: str) -> str:
    """Add account `open` (or close/commodity) directives to the book.

    Example: `2026-01-01 open Assets:Checking USD`
    """
    ledger = _require(_book())
    try:
        if _is_hosted(ledger):
            res = ledger.open_accounts(beancount_text)
        else:
            res = ledger.add_directives(beancount_text, "Open accounts via AI")
    except LedgerError as e:
        return f"REJECTED — ledger left unchanged.\n{e}"
    return f"Committed {res.commit}: {res.message}"


@mcp.tool()
def add_directives(beancount_text: str, message: str = "Update ledger via AI") -> str:
    """Append arbitrary Beancount directives (auto-routed by kind) and commit.

    Use for batch setup ("set up my whole situation"): opens, balances,
    transactions and prices in one validated commit.
    """
    ledger = _require(_book())
    try:
        res = ledger.add_directives(beancount_text, message)
    except LedgerError as e:
        return f"REJECTED — ledger left unchanged.\n{e}"
    return f"Committed {res.commit}: {res.message}"


# ---------------------------------------------------------------- reads ------
@mcp.tool()
def list_accounts() -> str:
    """List every account currently open in the book."""
    return "\n".join(_require(_book()).accounts()) or "(no accounts yet)"


@mcp.tool()
def get_ledger() -> str:
    """Return the full plain-text ledger (accounts, transactions, prices)."""
    return _require(_book()).read_all()


@mcp.tool()
def run_query(bql: str) -> str:
    """Run a Beancount Query Language (BQL) query and return CSV-style rows.

    Example: `SELECT account, sum(position) WHERE account ~ 'Expenses' GROUP BY account`
    """
    rows = _require(_book()).query(bql)
    if not rows:
        return "(no rows)"
    return json.dumps(rows, indent=2)


@mcp.tool()
def balances(account_filter: str = "") -> str:
    """Show balances grouped by account, optionally filtered by a regex.

    `account_filter` is a BQL regex like 'Assets' or 'Expenses:Food'.
    """
    where = f"WHERE account ~ '{account_filter}' " if account_filter else ""
    bql = f"SELECT account, sum(position) AS balance {where}GROUP BY account ORDER BY account"
    rows = _require(_book()).query(bql)
    if not rows:
        return "(no balances)"
    return "\n".join(f"{r.get('account',''):<40} {r.get('balance','')}" for r in rows)


# ------------------------------------------------------------- assessment ----
@mcp.tool()
def assess_book() -> str:
    """Review the book and return COMPUTED facts about it as JSON.

    Coverage, monthly income/expense, cash, run rate and runway, category
    shares, month-over-month movers, unusually large postings, and data-quality
    flags.

    Read this and report it. Do NOT compute your own figures from it, do not
    extrapolate past the coverage window, and do not turn a `sufficient: false`
    into a number with a caveat — that field means the data cannot support the
    figure, and the honest answer is to say which data is missing.

    Every month is marked `complete`. Only complete months are averaged: a
    trailing partial month makes spending look like it fell in every category.
    """
    book = _require(_book())
    try:
        facts = assessment.assess(book)
    except LedgerError as e:
        return f"Could not assess the book: {e}"
    return json.dumps(facts, indent=2)


# --------------------------------------------------------------- importing ---
#
# Two tools, and the split is #637's whole fix. `propose_transactions` parses a
# statement ON THE BOOK and hands back a bounded summary plus a `proposal_id`;
# `commit_proposal` writes that proposal from the parsed rows. The model never
# carries the rows in either direction: it did, and a statement over ~65 rows
# could not survive the trip — the copy in hit the 4,096-token output cap, and
# the 253 KB of rows out had to be re-typed into `add_transactions` by hand.
@mcp.tool()
def propose_transactions(
    content: str = "",
    account: str = "",
    currency: str = "",
    file_format: str = "auto",
    content_encoding: str = "auto",
    amount_shape: str = "auto",
    columns: str = "",
    date_format: str = "",
    delimiter: str = "",
    decimal_separator: str = "",
    sign: str = "auto",
    opening_balance: str = "",
    counter_account: str = "",
    statement_id: str = "",
    filename: str = "",
) -> str:
    """Parse a bank statement (CSV or OFX/QFX) into a PROPOSAL. Writes nothing.

    Give `statement_id` when you were given one (`stm_…`): the statement is
    already stored on the book, so do NOT ask for or copy the file. Otherwise
    give `content` — the statement's bytes, base64-encoded, or its text pasted
    straight in. NOT a path.

    `account` is required: the book account the statement belongs to, e.g.
    'Assets:Checking' or 'Liabilities:Visa'. Every row is one side of it and
    the file never says which.

    Returns a SUMMARY, not the rows: `proposal_id`, `counts`, `date_range`,
    `totals`, the column `mapping` (read it — it says what was assumed and on
    what evidence, including whether the running balance confirmed the sign),
    what was `flagged` and why, and `payees` — each distinct payee once, with
    how many rows and the net amount.

    WHAT TO DO NEXT
    - Tell the customer the counts, the date range and anything flagged.
    - If the mapping is wrong, call again with the matching override rather
      than fixing numbers yourself: amount_shape ('signed' | 'debit_credit' |
      'balance'), columns (JSON like {"date": "Posted Date", "amount":
      "Amount"}), date_format (strptime), delimiter, decimal_separator,
      sign ('normal' | 'inverted'), opening_balance, currency.
    - Categorise by PAYEE and call `commit_proposal`. Never re-type the rows
      into `add_transactions`.
    """
    book = _require(_book())
    options = dict(
        currency=currency, file_format=file_format, amount_shape=amount_shape,
        columns=columns, date_format=date_format, delimiter=delimiter,
        decimal_separator=decimal_separator, sign=sign,
        opening_balance=opening_balance, counter_account=counter_account,
    )
    if _is_hosted(book):
        if not statement_id:
            data = proposals.content_to_bytes(content, content_encoding)
            try:
                statement_id = book.stage_statement(
                    base64.b64encode(data).decode("ascii"), filename,
                )["statement_id"]
            except HostedNotFoundError:
                # 🔴 THE BOOK RUNS AN IMAGE FROM BEFORE #637 (PR #654 review).
                # This plugin publishes on every push to main; the books move
                # only on a manual rollout, and the pinned image has no
                # `/statements`. Failing here broke every hosted import that
                # worked before, so it does what it did before instead. If the
                # 404 meant "no such book", `read_all` raises the same error.
                return _propose_in_process_pre_637(book, data, account, options)
        summary = book.propose_statement(statement_id, account, options)
    else:
        if not statement_id:
            statement_id = proposals.stage_statement(
                book.root, proposals.content_to_bytes(content, content_encoding), filename,
            )["statement_id"]
        summary = proposals.propose(book.root, book.read_all(), statement_id, account, **options)
    # Compact on purpose: this string is re-sent on every later turn of the
    # session (#637), so indentation is a per-message cost with no reader.
    return json.dumps(summary, separators=(",", ":"), ensure_ascii=False)


def _propose_in_process_pre_637(book, data: bytes, account: str, options: dict) -> str:
    """The pre-#637 hosted import, for a book whose runtime lacks the routes.

    Parsed HERE, against the book's ledger text, and every row returned — the
    shape the old tool had, because the old write path is the only one such a
    book has: there is no ``commit_proposal`` on it, so the model writes the
    ``beancount`` block through ``add_transactions`` exactly as before. It
    keeps the old size ceiling; it does not lose the imports that worked.

    ⚠️ Delete once every book runs an image with `/statements` (and
    ``BOOK_RUNTIME_IMAGE`` in the control plane's fly.toml is repinned).
    """
    result = importing.propose(data, account, book_text=book.read_all(), **options)
    result["next_step"] = (
        "This book runs an older version that cannot store a statement, so "
        "there is no proposal_id and commit_proposal is not available on it. "
        "Open anything in `accounts_to_open` first (`open_directives` is ready "
        "for open_accounts), categorise the placeholder accounts in "
        "`beancount`, keep the "
        "`import-id:` lines and the `!` flags, and write it with "
        "add_transactions once the customer agrees."
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def commit_proposal(proposal_id: str, overrides: str = "", open_new_accounts: bool = False) -> str:
    """Write a proposal from `propose_transactions` into the book, as ONE commit.

    `overrides` is a JSON object with the accounts you chose:
      {"payees": {"REWE MARKT GMBH": "Expenses:Groceries",
                  "SALARY ACME LTD": "Income:Salary"},
       "rows":   {"cb1:0123abcd4567ef89": "Expenses:Travel"}}
    Payee keys are the `payee` values the proposal listed. `rows` overrides a
    single row by its import_id, and "skip" leaves that row (or that payee) out.
    Anything you do not name stays on Expenses:Unclassified /
    Income:Unclassified — say so to the customer rather than guessing.

    The transactions are written from the parsed rows: you choose accounts, you
    never type amounts. bean-check validates and git commits, as with every
    write; a rejected commit changes nothing. Rows already in the book are
    skipped, so calling this twice cannot double-book.

    If an account is not open yet the commit is refused and names it. Confirm
    the names with the customer, then call again with open_new_accounts=true to
    open them in the same commit.

    Only commit after the customer has agreed.
    """
    # ⚠️ RAISES ON REFUSAL, unlike `add_transactions`' catch-and-return, and on
    # purpose: MCP reports a raise as `isError: true`, so a caller branching on
    # the error flag cannot read a refused commit as a written one — the
    # divergence test_plugin_tool_parity.py records against the older writes.
    book = _require(_book())
    if _is_hosted(book):
        try:
            result = book.commit_proposal(proposal_id, overrides, open_new_accounts)
        except HostedNotFoundError as exc:
            # The new runtime answers an unknown proposal with 422, never 404,
            # so a 404 here is an older book image (or no access at all).
            raise LedgerError(
                f"{exc} If the book is reachable, it runs an older version "
                f"without commit_proposal: write the proposal through "
                f"add_transactions instead."
            ) from None
    else:
        result = proposals.commit(book, proposal_id, overrides, open_new_accounts)
    return json.dumps(result, indent=1)


# ---------------------------------------------------------------- reports ----
@mcp.tool()
def generate_report(format: str = "html", as_of: str = "", period_start: str = "") -> str:
    """Generate a financial report from the book.

    format: 'html' (styled, self-contained) or 'xlsx' (Excel workbook with
    Balance Sheet, Income Statement and Transactions sheets).
    as_of: balance-sheet date, YYYY-MM-DD (default today).
    period_start: first day of the income statement, YYYY-MM-DD (default
    1 January of the as_of year — the income statement is year to date).
    Returns the path to the written file.
    """
    ledger = _require(_book())
    if _is_hosted(ledger):
        return _hosted_report(ledger, format, as_of, period_start)
    out_dir = ledger.root / "reports"
    out_dir.mkdir(exist_ok=True)
    stamp = (as_of or "latest").replace(":", "-")
    fmt = format.lower()
    if fmt in ("html", "htm"):
        path = reporting.generate_html(ledger, str(out_dir / f"report-{stamp}.html"),
                                       as_of or None, period_start or None)
    elif fmt in ("xlsx", "excel"):
        path = reporting.generate_xlsx(ledger, str(out_dir / f"report-{stamp}.xlsx"),
                                       as_of or None, period_start or None)
    else:
        return f"Unknown format '{format}'. Use 'html' or 'xlsx'."
    return f"Report written to {path}"


def _hosted_report(ledger, format: str, as_of: str, period_start: str = "") -> str:
    """Render on the book, save beside the user's other reports.

    The runtime returns HTML inline or xlsx bytes (§4 row 7), so the file is
    written here rather than on the machine — the customer asked for a report,
    not for a path they cannot reach.
    """
    fmt = (format or "html").lower()
    if fmt not in ("html", "htm", "xlsx", "excel"):
        return f"Unknown format '{format}'. Use 'html' or 'xlsx'."
    body = {"format": fmt}
    if as_of:
        body["as_of"] = as_of
    if period_start:
        # ⚠️ A book image older than #638 drops the key (pydantic ignores
        # unknown fields) and renders its old since-inception P&L, whose
        # header names no period. Only the new header says which one it is.
        body["period_start"] = period_start
    try:
        raw = ledger.report_bytes(body)
    except LedgerError as e:
        return f"Could not generate the report: {e}"
    out_dir = Path.home() / ".countbean" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (as_of or "latest").replace(":", "-")
    suffix = "html" if fmt in ("html", "htm") else "xlsx"
    path = out_dir / f"report-{stamp}.{suffix}"
    path.write_bytes(raw)
    return f"Report written to {path}"


# --------------------------------------------------------------- receipts ----
#
# Two tools, not one, and the split is the whole design (#516).
#
# `stage_receipt` moves bytes and returns a REFERENCE. `propose_receipt_transaction`
# turns a reading of those bytes into a proposal. Between them sits the one
# thing this product will never do server-side: the OCR. #516 ruled that the
# reading is the AGENT'S OWN VISION TURN — on the chat path the photo is already
# an inline image part on the metered endpoint (CONTRACTS §3.10) and the model
# has it in context; on the plugin path Claude Code reads the file and we are
# billed nothing at all. Measured at $0.00087 per receipt photo, and a 3024x4032
# phone photo and a 900x1200 downscale produce identical prompt tokens (1,103)
# because the model normalises the image, so downscaling saves upload bandwidth
# and not money.
#
# ⛔ NEITHER TOOL WRITES. `add_transactions` remains the only write path, so the
# bean-check gate and the git commit stay exactly where they are.


@mcp.tool()
def stage_receipt(
    file_path: str = "", content_base64: str = "", declared_name: str = ""
) -> str:
    """Store a receipt photo or PDF as evidence and return a reference to it.

    Call this FIRST, before proposing anything. A receipt is the source
    document for the entry it becomes, and an entry whose evidence was thrown
    away is a promise we cannot keep two years from now.

    Give it EITHER `file_path` (a photo or PDF on this machine — the plugin
    path) OR `content_base64` (raw bytes, for a chat agent that has no
    filesystem in common with the book). `declared_name` is recorded in the
    reply for your convenience and is used for NOTHING else — never for the
    file type, never for where the bytes are stored.

    Returns JSON with the bucket key and the content hash. Pass both to
    `propose_receipt_transaction`. Then read the image yourself: this tool does
    not look at it.
    """
    try:
        data = receipts.read_input(file_path, content_base64)
    except receipts.ReceiptInputError as e:
        return json.dumps({"error": str(e)}, indent=2)

    media = receipts.sniff_content_type(data)
    if media not in receipts.STORABLE:
        return json.dumps({
            "error": (
                f"That does not look like a receipt image or PDF "
                f"({media or 'unrecognised file type'}). Storable types are "
                f"{', '.join(sorted(receipts.STORABLE))}. The type is read "
                f"from the bytes, not the filename."
            )
        }, indent=2)

    book = _require(_book())
    try:
        stored = receipts.store(book, data, media, hosted=_is_hosted(book))
    except LedgerError as e:
        return json.dumps({"error": f"The receipt was NOT stored: {e}"}, indent=2)

    return json.dumps({
        "receipt": stored,
        "declared_name": declared_name,
        "readable_by_model": media != "application/pdf",
        "next_step": receipts.next_step_for(media),
    }, indent=2)


@mcp.tool()
def propose_receipt_transaction(
    receipt_sha256: str,
    receipt_key: str = "",
    date: str = "",
    date_confidence: float = 0.0,
    merchant: str = "",
    merchant_confidence: float = 0.0,
    total: str = "",
    total_confidence: float = 0.0,
    tax: str = "",
    tax_confidence: float = 0.0,
    currency: str = "",
    currency_confidence: float = 0.0,
    line_items_json: str = "",
    extracted_text: str = "",
    expense_account: str = "",
    paid_from_account: str = "",
    operating_currency: str = "",
    exchange_rate: str = "",
    confidence_floor: float = 0.6,
) -> str:
    """Turn what you read off a staged receipt into a PROPOSED transaction.

    You supply the reading and, for every field, how sure you are of it on a
    0.0-1.0 scale. Be honest about the confidences — they are the whole
    mechanism. **Anything below the floor comes back flagged rather than as a
    value**, and the floor can be raised by argument but never lowered, so a
    low confidence is not a suggestion.

    `expense_account` and `paid_from_account` are yours to choose:
    categorisation is the part of this a model is genuinely good at. The book's
    `open` directives are read here so that two failures which would otherwise
    land AT THE WRITE — an account that was never opened, and an account pinned
    to a currency this receipt is not in — arrive now, as a sentence, instead of
    as a bean-check dump after the customer has already approved the entry.

    `exchange_rate` is how much ONE unit of the receipt's currency cost in the
    paying account's currency. Take it off the card statement; no rate is ever
    invented here.

    Returns JSON. It NEVER writes. Show the person the merchant, the date and
    the total, ask about anything flagged, and only then pass `beancount` to
    `add_transactions` unchanged.
    """
    book = _require(_book())
    try:
        items = json.loads(line_items_json) if line_items_json.strip() else []
        if not isinstance(items, list):
            raise ValueError("line_items_json must be a JSON array")
    except (ValueError, TypeError) as e:
        return json.dumps({"error": f"line_items_json is not usable: {e}"}, indent=2)

    # Read the chart of accounts so the two write-time failures become
    # proposal-time ones. A failure to read it is NOT fatal — the proposal is
    # still worth having — but it is reported, because "not checked" and
    # "checked and fine" must never look the same.
    try:
        constraints = receipts.open_currency_constraints(book.read_all())
    except LedgerError:
        constraints = None

    proposal = receipts.build_proposal(
        receipt={"key": receipt_key, "sha256": receipt_sha256},
        date=date, date_confidence=date_confidence,
        merchant=merchant, merchant_confidence=merchant_confidence,
        total=total, total_confidence=total_confidence,
        tax=tax, tax_confidence=tax_confidence,
        currency=currency, currency_confidence=currency_confidence,
        line_items=items,
        extracted_text=extracted_text,
        expense_account=expense_account,
        paid_from_account=paid_from_account,
        operating_currency=operating_currency,
        exchange_rate=exchange_rate,
        account_currencies=constraints,
        confidence_floor=confidence_floor,
    )
    return json.dumps(proposal, indent=2)


# ---------------------------------------------------------------- history ----
@mcp.tool()
def history(limit: int = 20) -> str:
    """Show the book's git history (each AI or human change is one commit)."""
    hist = _require(_book()).history(limit)
    return "\n".join(f"{h['commit']} {h['date']} {h['message']}" for h in hist) \
        or "(no history)"


@mcp.tool()
def revert(commit: str) -> str:
    """Revert a specific commit (undo a change), creating a new commit."""
    res = _require(_book()).revert(commit)
    return f"Reverted {commit} → {res.commit}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
