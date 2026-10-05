"""Report generation for a finkpr book: the paper HTML report and Excel (.xlsx).

Both formats are built from the same numbers (``collect``) so they always agree.

HOW IT LOOKS (the finkpr redesign, A50 — finkpr-brand ``redesign/a50-report``):
a report is a DOCUMENT, so it sits on paper in both the reader's modes (the old
page followed ``prefers-color-scheme`` into dark, which a document never does).
Figures are IBM Plex Mono with tabular numerals. The balance sheet and the
income statement are in natural presentation — liabilities, equity and income
read positive, a negative is hung in parentheses — and stay in ink: ledger
green and red colour only a SIGNED result (profit for the period), and always
with its ``+`` or its parentheses, so colour is never the only channel. Every
colour, face and the wordmark come from ``brand_tokens`` (generated from
platform/packages/brand/tokens.json); none is typed here.

WHAT THIS FILE USED TO GET WRONG (#638), all measured on one five-line
adversarial ledger (tests/fixtures/report_adversarial/) before the fix:

1. **It read numbers off rendered text.** Every figure came from bean-query's
   CSV output, and ``_amount`` took the FIRST number in the cell. A cell holding
   ``4000.00 GBP, 100.00 USD`` became ``4000.00`` — the GBP quantity, reported
   as dollars, with the real 100 USD dropped. Rendered text is also quantized to
   the book's display precision (``-5.5 EUR`` prints as ``-6 EUR``) and, for
   VND-scale quantities, crashes bean-query's renderer outright. Numbers now
   come from beanquery's Python API as ``Decimal`` per currency.
2. **``convert(position, 'USD')`` without a date values at the LATEST price.**
   A report "as of 2024-12-31" valued 1,000 EUR at a rate first published on
   2025-06-30: 2,000.00 instead of 1,000.00. The balance sheet now converts at
   ``as_of`` (the closing rate) and the income statement at each posting's own
   date — beanquery's ``convert(position, ccy, date)``, which is hledger's
   ``--value=then`` to the cent.
3. **The income statement ran from inception.** "Net income" as of 2025-12-31
   included 2024's sales. It now covers ``period_start``..``as_of``, and
   ``period_start`` defaults to 1 January of the ``as_of`` year.
   🔴 Cutting the P&L at ``period_start`` is only half of that fix. Beancount
   has no closing entries unless a query asks for them, so every profit booked
   before ``period_start`` still sits in Income/Expenses — and a year-to-date
   P&L drops it. The first cut of #638 shipped that way: a USD-only book with
   a 5,000 sale in 2025 and 1,000 of rent in 2026 reported, as of 2026-06-30,
   assets 4,000, equity 0, net income -1,000 — the 5,000 was on no line, and
   from every 1 January onward that is EVERY book with a prior year, single
   currency or not. The balance sheet therefore carries one more Equity row,
   ``Retained earnings (before <period_start>)``: Income + Expenses dated
   before the period, each posting at its own date's rate (the same rate the
   P&L that earned it would have used).
4. An amount with no price into the report currency is never summed into it.
   It is kept, labelled with its own currency, and listed in ``unconverted`` —
   a total that silently includes GBP-as-dollars is the defect in (1) again.

With that row a single-currency book ties to the cent: Assets + Liabilities
+ Equity + Income + Expenses = 0 (test_reporting pins it).

⚠️ Balance-sheet items at the closing rate and P&L items (and retained
earnings) at transaction-date rates is IAS 21 / ASC 830 practice, and it means
a book holding a second currency whose rate MOVED does not tie: the gap is the
unrealised FX (translation) difference, which this report does not yet post as
a line. That is a Phase 1 item (the report-data contract), not a bug in this
file. A single-currency book has no such gap.

🔴 THIS FILE EXISTS TWICE. ``plugin/mcp/countbean_mcp/reporting.py`` is a
byte-for-byte copy — the plugin ships standalone and cannot import the monorepo
— and ``book-runtime/tests/test_no_ledger_drift.py`` fails the build if they
differ. Edit this one, then ``cp`` it over. Nothing below may depend on the
package's import name for the same reason (see ``_child_argv``).
"""
from __future__ import annotations

import datetime
import html
import json
import sys
from decimal import Decimal
from pathlib import Path

from .brand_tokens import (
    FONT_BODY, FONT_DISPLAY, FONT_FILES, FONT_MONO, FONT_UNICODE_RANGE, LIGHT,
    WORDMARK_SYMBOL, xl,
)
from .ledger import Ledger, LedgerError

# Root names the report sections by. Beancount lets a book rename them
# (`option "name_assets"`); no Countbean book does, and the templates assume these.
BALANCE_ROOTS = ("Assets", "Liabilities", "Equity")
PNL_ROOTS = ("Income", "Expenses")

CENT = Decimal("0.01")

# How many postings of the period the journal carries. A report used to show
# the latest 100 lines of the book's whole life; it now shows the period's
# entries, grouped per entry, and says so when it had to stop early.
TXN_LIMIT = 500


def _iso_date(value: str, name: str) -> str:
    """A YYYY-MM-DD date or a LedgerError (→ 422) — never BQL text.

    Both dates are interpolated into a query, and ``as_of`` arrives straight
    from ``POST /report``'s body. The old code interpolated it unchecked, so
    ``as_of`` was a BQL injection point that happened to be read-only.
    """
    try:
        parsed = datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise LedgerError(f"{name} must be a date like 2026-03-31, got {value!r}.") from None
    if parsed.isoformat() != value:  # fromisoformat also takes 20260331
        raise LedgerError(f"{name} must be a date like 2026-03-31, got {value!r}.")
    return value


# ------------------------------------------------------- the query child ----
def _query_book(main: str, as_of: str, period_start: str) -> dict:
    """Load the book once, run the report's three queries, return plain JSON.

    Runs in a CHILD process (see ``_run_queries``), never in the server: a
    tenant machine has 256 MB, and holding a whole parsed book in the
    long-lived FastAPI process would keep that memory for the life of the
    process. The child also keeps #172's stall budget, which bean-query had.

    Numbers leave as ``str(Decimal)`` per currency, so nothing is rounded,
    quantized to a display precision, or merged across currencies here.
    """
    import datetime as _dt

    from beancount import loader
    from beanquery import query

    as_of_d = _dt.date.fromisoformat(as_of)
    start_d = _dt.date.fromisoformat(period_start)
    # Loader errors are what bean-check is for; bean-query ignored them too,
    # and a report on a book with one bad line is still worth having.
    entries, _errors, options = loader.load_file(main)
    currencies = options.get("operating_currency") or []
    ccy = currencies[0] if currencies else "USD"

    def run(bql):
        _types, rows = query.run_query(entries, options, bql)
        return rows

    def positions(inv):
        # Positions of a sum of convert() results carry no cost: convert
        # returns an Amount, so each entry is one (number, currency) pair.
        if inv is None:
            return []
        return [[str(p.units.number), p.units.currency] for p in inv if p.units.number]

    def roots(names):
        return "^(" + "|".join(names) + ")(:|$)"

    # Balance sheet: everything up to as_of, valued at the as_of (closing) rate.
    # `units` beside the converted balance, so the report can print the
    # original amount (98,500.00 EUR) and the rate it was valued at.
    balance = run(
        f"SELECT account, sum(units(position)) AS units, "
        f"sum(convert(position, '{ccy}', {as_of_d})) AS balance "
        f"WHERE account ~ '{roots(BALANCE_ROOTS)}' AND date <= {as_of_d} "
        "GROUP BY account ORDER BY account"
    )
    # Income statement: the period only, each posting at its own date's rate.
    pnl = run(
        f"SELECT account, sum(convert(position, '{ccy}', date)) AS balance "
        f"WHERE account ~ '{roots(PNL_ROOTS)}' "
        f"AND date >= {start_d} AND date <= {as_of_d} "
        "GROUP BY account ORDER BY account"
    )
    # Retained earnings: every Income/Expenses posting BEFORE the period, which
    # the P&L above no longer shows and nothing else would (see the module
    # docstring, 3). One row, no GROUP BY: it is a single Equity figure.
    retained = run(
        f"SELECT sum(convert(position, '{ccy}', date)) AS balance "
        f"WHERE account ~ '{roots(PNL_ROOTS)}' AND date < {start_d}"
    )
    # The period's entries, one row per posting; `id` groups postings back into
    # their entry and `flag` carries Beancount's `!` (an entry waiting for an
    # answer). One more row than the limit is asked for, so a cut is known.
    txns = run(
        f"SELECT id, date, flag, payee, narration, account, units(position) AS units, "
        f"convert(position, '{ccy}', date) AS amount "
        f"WHERE date >= {start_d} AND date <= {as_of_d} "
        f"ORDER BY date DESC, id LIMIT {TXN_LIMIT + 1}"
    )
    cut = len(txns) > TXN_LIMIT
    if cut:
        # Never print half an entry: drop the last one the limit split.
        last = txns[TXN_LIMIT][0]
        txns = [t for t in txns[:TXN_LIMIT] if t[0] != last]
    return {
        "currency": ccy,
        "balance": [[acct, positions(inv)] for acct, _units, inv in balance],
        "units": [[acct, positions(units)] for acct, units, _inv in balance],
        "pnl": [[acct, positions(inv)] for acct, inv in pnl],
        # An aggregate over no rows is still one row, holding None or an empty
        # inventory; `positions` makes both [].
        "retained": positions(retained[0][0]) if retained else [],
        "transactions": [
            [d.isoformat(), payee or "", narration or "", acct,
             str(amt.number) if amt is not None else "0",
             amt.currency if amt is not None else ccy,
             entry_id, flag or "",
             str(units.number) if units is not None else "0",
             units.currency if units is not None else ccy]
            for entry_id, d, flag, payee, narration, acct, units, amt in txns
        ],
        "transactions_cut": cut,
    }


def _child_argv(main: str, as_of: str, period_start: str) -> list[str]:
    """The command that runs ``_query_book`` for this copy of this file.

    Imported by DIRECTORY name, not ``__name__``: the same bytes are
    ``ledger_core.reporting`` in the runtime and ``countbean_mcp.reporting`` in
    the plugin, and the plugin's tests load it under a synthetic package name
    that does not exist on disk. The directory is always real.

    🔴 ``-P`` is load-bearing. ``_run`` starts the child in the BOOK directory,
    and ``python -c`` puts the cwd ('') on sys.path ahead of the standard
    library — so without ``-P`` a ``json.py`` or ``csv.py`` in the book root
    ran inside the report (measured: the report failed with that file's
    traceback). The old ``bean-query`` console script never had the cwd on its
    path. A book is data, not configuration (#105); a local plugin user's
    ledger directory routinely holds importer scripts. ``-P`` is Python 3.11+,
    which is ledger-core's ``requires-python`` and the plugin's run.sh floor.
    """
    here = Path(__file__).resolve()
    module = f"{here.parent.name}.{here.stem}"
    bootstrap = (
        "import json, sys, importlib; "
        "sys.path.insert(0, sys.argv[1]); "
        "m = importlib.import_module(sys.argv[2]); "
        "json.dump(m._query_book(*sys.argv[3:6]), sys.stdout)"
    )
    return [sys.executable, "-P", "-c", bootstrap, str(here.parent.parent), module,
            main, as_of, period_start]


def _run_queries(ledger: Ledger, as_of: str, period_start: str) -> dict:
    # `_run` applies BEAN_TIMEOUT to anything that is not git — the same stall
    # detector bean-query ran under — and turns a timeout into a 500, not a 422.
    proc = ledger._run(*_child_argv(str(ledger.main), as_of, period_start), check=False)
    if proc.returncode != 0:
        # The old code swallowed every query failure into an empty section, so
        # a broken query produced a report of zeros that looked like a book
        # with nothing in it. A report that cannot be computed says so.
        tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-5:])
        raise LedgerError("Could not compute the report:\n" + tail)
    return json.loads(proc.stdout)


# -------------------------------------------------------------- assembly ----
def _split(positions: list, ccy: str) -> tuple[Decimal, list[dict]]:
    """Report-currency amount, plus every other currency kept apart."""
    amount = Decimal(0)
    other = []
    for number, currency in positions:
        if currency == ccy:
            amount += Decimal(number)
        else:
            other.append({"currency": currency, "amount": Decimal(number)})
    return amount, other


def _money(d: Decimal) -> float:
    # Rounded to the cent BEFORE anything sums it, so a total always equals
    # the rows printed above it. Float only at the edge, for openpyxl and
    # JSON; every sum is Decimal.
    return float(d.quantize(CENT))


def collect(ledger: Ledger, as_of: str | None = None, period_start: str | None = None) -> dict:
    date = _iso_date(as_of, "as_of") if as_of else datetime.date.today().isoformat()
    start = _iso_date(period_start, "period_start") if period_start else f"{date[:4]}-01-01"
    if start > date:
        raise LedgerError(f"period_start ({start}) is after as_of ({date}).")

    raw = _run_queries(ledger, date, start)
    ccy = raw["currency"]
    sections: dict[str, list[dict]] = {r: [] for r in BALANCE_ROOTS + PNL_ROOTS}
    t = {r.lower(): Decimal(0) for r in BALANCE_ROOTS + PNL_ROOTS}
    unconverted: list[dict] = []

    def add(root: str, account: str, positions: list) -> None:
        amount, other = _split(positions, ccy)
        t[root.lower()] += amount.quantize(CENT)
        row = {
            "account": account,
            "amount": _money(amount),
            "unconverted": [
                {"currency": o["currency"], "amount": float(o["amount"])} for o in other
            ],
        }
        sections[root].append(row)
        unconverted.extend({"account": account, **o} for o in row["unconverted"])

    for account, positions in raw["balance"] + raw["pnl"]:
        add(account.split(":", 1)[0], account, positions)
    # The original amounts behind each balance-sheet line, in every currency
    # the account holds. Rendering only; no total reads them.
    held = {acct: units for acct, units in raw.get("units", [])}
    for root in BALANCE_ROOTS:
        for row in sections[root]:
            row["units"] = [
                {"currency": c, "amount": float(Decimal(n))}
                for n, c in held.get(row["account"], [])
            ]
    # Profit (or loss) from before the period, as Equity — the sign is the
    # book's own (a profit is a credit, negative, like Equity:Opening-Balances
    # above it). Omitted when there is none, e.g. a book opened this year.
    if raw["retained"]:
        add("Equity", f"Retained earnings (before {start})", raw["retained"])

    txns = [
        {"date": d, "payee": payee, "narration": narration, "account": acct,
         "amount": _money(Decimal(number)), "currency": currency,
         "id": entry_id, "flag": flag,
         "units": float(Decimal(u_number)), "units_currency": u_currency}
        for (d, payee, narration, acct, number, currency,
             entry_id, flag, u_number, u_currency) in raw["transactions"]
    ]

    return {
        "title": _title(ledger),
        "currency": ccy,
        "as_of": date,
        "period_start": start,
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "assets": sections["Assets"], "liabilities": sections["Liabilities"],
        "equity": sections["Equity"], "income": sections["Income"],
        "expenses": sections["Expenses"], "transactions": txns,
        "transactions_cut": raw.get("transactions_cut", False),
        # The version of the book this report was computed from: its latest
        # commit, short id and date. None when the book has no history to cite.
        "version": _version(ledger),
        # Amounts with no price into `currency` on the date that mattered. In
        # NO total below — listed so every renderer can say what it left out.
        "unconverted": unconverted,
        "totals": {
            **{k: float(v) for k, v in t.items()},
            "net_worth": float(t["assets"] + t["liabilities"]),
            "net_income": float(-(t["income"] + t["expenses"])),
        },
    }


def _title(ledger: Ledger) -> str:
    for line in ledger.main.read_text().splitlines():
        if line.startswith('option "title"'):
            parts = line.split('"')
            if len(parts) >= 4:
                return parts[3]
    return "Your book"


def _version(ledger: Ledger) -> dict | None:
    # A report cites the state it was read from, so an accountant can name
    # the exact version they reviewed. A book without git history (a local
    # plugin book someone assembled by hand) still gets its report.
    try:
        head = ledger.history(1)
    except Exception:  # noqa: BLE001 - the report must not fail on its footer
        return None
    return {"id": head[0]["commit"], "date": head[0]["date"]} if head else None




# ---------------------------------------------------------- presentation ----
# Shared by the HTML page and the workbook, so both print the same words.
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

# Roots whose balances read positive in natural presentation: the book keeps
# them as credits (negative), a reader expects liabilities, equity and income
# as positive figures. Only the renderers flip; `collect` keeps the book's sign.
_CREDIT_ROOTS = ("Liabilities", "Equity", "Income")


def _dec(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT)


def _natural(root: str, amount) -> Decimal:
    v = _dec(amount)
    return -v if root in _CREDIT_ROOTS else v


def _flip(root: str, amount: float) -> float:
    # _natural for an amount that must keep every place it was booked with.
    return -amount if root in _CREDIT_ROOTS else amount


def _day(iso: str) -> str:
    d = datetime.date.fromisoformat(iso)
    return f"{d.day} {_MONTHS[d.month - 1][:3]} {d.year}"


def _period_names(start: str, end: str) -> tuple[str, str]:
    """(the report's title, the income statement's column head)."""
    s, e = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    if s.year == e.year and s.day == 1:
        if s.month == e.month:
            return f"{_MONTHS[s.month - 1]} {s.year}", _MONTHS[s.month - 1]
        if s.month == 1 and e.month == 12 and e.day == 31:
            return str(s.year), str(s.year)
        return (f"{_MONTHS[s.month - 1]} – {_MONTHS[e.month - 1]} {s.year}",
                f"{_MONTHS[s.month - 1][:3]} – {_MONTHS[e.month - 1][:3]}")
    return f"{_day(start)} – {_day(end)}", "Period"


def _short(account: str) -> str:
    # Inside a section headed "Assets", "Bank:Wise" says it; the root is noise.
    return account.split(":", 1)[1] if ":" in account else account


def _foreign(amount: float, currency: str) -> str:
    # Enough places for the amount as booked — 0.12345678 BTC must not print
    # as 0.12 — and always the currency code beside it, because a bare 4,000.00
    # in a dollar report reads as dollars.
    return f"{amount:,.{max(2, -Decimal(str(amount)).as_tuple().exponent)}f} {currency}"


def _held(row: dict, ccy: str, root: str = "Assets") -> list[dict]:
    """The balance-sheet line's holdings in currencies other than the report's,
    each with the rate it was valued at, or None when it had no rate. Amounts
    are in natural presentation for ``root``, like the line's own figure."""
    foreign = [u for u in row.get("units", []) if u["currency"] != ccy and u["amount"]]
    missing = {o["currency"] for o in row["unconverted"]}
    only = len(row.get("units", [])) == 1 and len(foreign) == 1
    out = []
    for u in foreign:
        rate = None
        # A rate can be read back exactly only when the line holds this one
        # currency and it converted: then value = units × price, to the digit.
        if only and u["currency"] not in missing and row["amount"]:
            rate = (Decimal(str(row["amount"])) / Decimal(str(u["amount"]))).quantize(
                Decimal("0.0001"))
        amount = -u["amount"] if root in _CREDIT_ROOTS else u["amount"]
        out.append({**u, "amount": amount, "rate": rate})
    return out


def _entries(d: dict) -> list[dict]:
    """The journal rows grouped back into entries, oldest first."""
    by_id: dict[str, dict] = {}
    for t in d["transactions"]:
        e = by_id.get(t["id"])
        if e is None:
            e = by_id[t["id"]] = {"id": t["id"], "date": t["date"], "flag": t["flag"],
                                  "payee": t["payee"], "narration": t["narration"],
                                  "postings": []}
        e["postings"].append(t)
    return list(reversed(list(by_id.values())))


def _statement(d: dict) -> dict:
    """Every figure both renderers print, in natural presentation, and the
    check that the balance sheet closes."""
    ccy = d["currency"]
    t = d["totals"]
    profit = _dec(t["net_income"])
    assets = _dec(t["assets"])
    liabilities = -_dec(t["liabilities"])
    equity = -_dec(t["equity"]) + profit
    income = -_dec(t["income"])
    expenses = _dec(t["expenses"])
    gap = assets - (liabilities + equity)
    title, column = _period_names(d["period_start"], d["as_of"])
    empty = not any(d[k] for k in ("assets", "liabilities", "equity", "income", "expenses",
                                   "transactions"))
    return {
        "ccy": ccy, "title": title, "column": column, "profit": profit,
        "assets": assets, "liabilities": liabilities, "equity": equity,
        "income": income, "expenses": expenses, "net_assets": _dec(t["net_worth"]),
        "gap": gap, "empty": empty,
        "foreign": any(_held(r, ccy) for k in BALANCE_ROOTS for r in d[k.lower()]),
        "flagged": [e for e in _entries(d) if e["flag"] == "!"],
        "version": (d.get("version") or {}).get("id"),
        "prepared": d["generated"],
    }


# ---------------------------------------------------------------- HTML -------
def _num(v, signed: bool = False, unit: str | None = None) -> str:
    """A figure in the document convention: negatives hung in parentheses,
    positives plain. A SIGNED result also takes ledger colour, with a leading
    + when positive, so the sign and the colour always say the same thing."""
    v = _dec(v)
    tail = f' <span class="ccy">{html.escape(unit)}</span>' if unit else ""
    if v == 0:
        return f'<span class="n zero">0.00</span>{tail}'
    if v < 0:
        cls = "n neg paren" if signed else "n paren"
        return f'<span class="{cls}">({abs(v):,.2f})</span>{tail}'
    if signed:
        return f'<span class="n pos">+{v:,.2f}</span>{tail}'
    return f'<span class="n">{v:,.2f}</span>{tail}'


def _dash(why: str = "") -> str:
    t = f' title="{html.escape(why)}"' if why else ""
    return f'<span class="n zero"{t}>—</span>'


def _status(kind: str, size: int, word: str = "") -> str:
    w = f'<span class="pword">{html.escape(word)}</span>' if word else ""
    return (f'<span class="status"><span class="period period--{kind} p{size}" '
            f'aria-hidden="true"></span>{w}</span>')


def _acct(a: str) -> str:
    # Break a long account only after a colon, never inside a name.
    return html.escape(a).replace(":", ":<wbr>")


def _font_faces() -> str:
    """The brand faces, embedded, so the file renders the same with no network
    (it is forwarded, opened from a chat, printed). Absent files fall back to
    the stacks' system faces; Plex 600 is not used on the page."""
    import base64

    here = Path(__file__).resolve().parent / "fonts"
    out = []
    for family, file, weight, fmt in FONT_FILES:
        if "600" in weight and family == "IBM Plex Mono":
            continue
        path = here / file
        if not path.is_file():
            continue
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        out.append(
            f'@font-face {{ font-family: "{family}"; font-weight: {weight}; font-style: normal; '
            f"font-display: swap; unicode-range: {FONT_UNICODE_RANGE}; "
            f'src: url(data:font/woff2;base64,{data}) format("{fmt}"); }}'
        )
    return "\n".join(out)


def _root_css() -> str:
    roles = "\n".join(f"  --{k.replace('_', '-')}: {v};" for k, v in LIGHT.items())
    return (
        ":root {\n  color-scheme: light;\n" + roles + "\n"
        f"  --display: {FONT_DISPLAY};\n  --body: {FONT_BODY};\n  --mono: {FONT_MONO};\n"
        "  --t5: var(--interactive);\n  --close: 2px;\n  --radius: 2px;\n  --target: 44px;\n}"
    )


def generate_html(
    ledger: Ledger, out_path: str, as_of: str | None = None, period_start: str | None = None
) -> str:
    Path(out_path).write_text(render_html(collect(ledger, as_of, period_start)))
    return out_path


def render_html(d: dict) -> str:
    s = _statement(d)
    c = s["ccy"]
    esc = html.escape
    start, end = d["period_start"], d["as_of"]
    version = s["version"]
    prepared_day = s["prepared"][:10]

    meta = [("Book", esc(d["title"])), ("Period", f"{start} – {end}"), ("Currency", esc(c))]
    if version:
        meta.append(("Version", esc(version)))
    letterhead = (
        '<header class="letterhead"><svg class="wm" viewBox="0 0 3365 1133" width="128" '
        'height="43" role="img" aria-label="finkpr"><use href="#fk-wm"/></svg>'
        '<dl class="meta">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in meta)
        + "</dl></header>"
    )
    status_meta = ("no entries yet" if s["empty"]
                   else f"prepared by finkpr · {prepared_day} · not yet reviewed")
    head = (
        f'<div class="title"><span class="eyebrow">Report · {start} – {end}</span>'
        f'<h1>{esc(s["title"])}<span class="acc">.</span></h1>'
        f'<div class="statusline">{_status("idle", 12, "Open")}'
        f'<span class="meta2">{esc(status_meta)}</span></div>'
        + ("" if s["empty"] else
           f'<p class="lead">Balance sheet, income statement, and every transaction for '
           f'{esc(d["title"])} — in {esc(c)}.</p>')
        + "</div>"
    )
    foot = (
        f'<footer class="foot"><span class="l">Generated by finkpr'
        f'{" · version " + esc(version) if version else ""} · {prepared_day}</span>'
        "<p>Every number is up to date and is automatically checked for accuracy, "
        "with a full history of every change.</p></footer>"
    )

    if s["empty"]:
        kpis = '<div class="kpis" style="--k:2">' + "".join(
            f'<div class="kpi"><span class="eyebrow">{k}</span><div class="fig">—</div>'
            '<p class="why">No entries yet</p></div>'
            for k in (f"Net assets · {_day(end)[:-5]}", f"Profit · {esc(s['column'])}")
        ) + "</div>"
        body = (
            letterhead + head + kpis
            + '<div class="empty-state"><p>Your book has nothing in it yet, so there is not '
              'much to report.</p><p class="why">Tell the bot what you spent, or send a '
              'statement — the report fills in as entries are recorded.</p></div>' + foot
        )
        return _page(f'{d["title"]} — {s["title"]}', body)

    # --- the figures band
    cash = [r for r in d["assets"]
            if r["account"].startswith(("Assets:Bank", "Assets:Cash"))]
    kpi = [
        (f"Net assets · {_day(end)[:-5]}", _num(s["net_assets"]),
         f'assets {s["assets"]:,.2f} · liabilities {s["liabilities"]:,.2f}'),
        (f"Profit · {esc(s['column'])}", _num(s["profit"], signed=True),
         f'income {s["income"]:,.2f} · expenses {s["expenses"]:,.2f}'),
    ]
    if cash:
        held = [h for r in cash for h in _held(r, c)]
        sub = ("held as " + " · ".join(_foreign(h["amount"], h["currency"]) for h in held)
               if held else f"all in {c}")
        kpi.append((f"Cash at bank · {_day(end)[:-5]}",
                    _num(sum((_dec(r["amount"]) for r in cash), Decimal(0))), sub))
    kpis = f'<div class="kpis" style="--k:{len(kpi)}">' + "".join(
        f'<div class="kpi"><span class="eyebrow">{k}</span><div class="fig">{v}</div>'
        f'<div class="sub">{esc(sub)}</div></div>' for k, v, sub in kpi
    ) + "</div>"

    # --- where it went: the period's largest expenses, drawn as bars
    spent = sorted(((r["account"], _dec(r["amount"])) for r in d["expenses"]
                    if _dec(r["amount"]) > 0), key=lambda x: -x[1])
    chart = ""
    if spent:
        top, rest = spent[:6], spent[6:]
        if rest:
            top.append(("Other", sum((v for _, v in rest), Decimal(0))))
        mx = max(v for _, v in top)
        bars = "".join(
            f'<div class="hb"><span class="a" title="{esc(a)}">{esc(_short(a))}</span>'
            f'<span class="t"><i style="width:{max(1.0, float(v / mx) * 100):.1f}%"></i></span>'
            f"{_num(v)}</div>" for a, v in top
        )
        chart = (f'<div class="charts"><div class="chart"><span class="eyebrow">Where it went · '
                 f'{esc(s["column"])} · {esc(c)}</span><div class="hbars">{bars}</div></div></div>')

    # --- balance sheet
    fx = s["foreign"]
    span = 4 if fx else 2
    wide = '<td class="w"></td><td class="w"></td>' if fx else ""

    def bs_row(root: str, label: str, row: dict | None, value: Decimal, sup: str = "") -> str:
        orig = rate = om = ""
        if row is not None and fx:
            held = _held(row, c, root)
            orig = "<br>".join(_num(h["amount"], unit=h["currency"]) for h in held)
            rate = "<br>".join(
                f'<span class="n">{h["rate"]}</span>' if h["rate"] is not None
                else '<span class="n zero">no rate</span>' for h in held)
            om = "".join(
                f'<span class="om">{esc(_foreign(h["amount"], h["currency"]))} @ '
                f'{h["rate"] if h["rate"] is not None else "no rate"}</span>' for h in held)
        cells = f'<td class="r w">{orig}</td><td class="r w">{rate}</td>' if fx else ""
        # A line whose only holding had no rate has no figure in the report
        # currency: an em dash, not a 0.00 that reads as an empty account.
        shown = (_dash("no rate") if row is not None and value == 0 and row["unconverted"]
                 else _num(value))
        return (f'<tr><td class="acct">{_acct(label)}{sup}{om}</td>{cells}'
                f'<td class="r">{shown}</td></tr>')

    rows = [f'<tr class="grp"><td colspan="{span}">Assets</td></tr>']
    rows += [bs_row("Assets", _short(r["account"]), r, _natural("Assets", r["amount"]))
             for r in d["assets"]]
    rows.append(f'<tr class="tot"><td>Total assets</td>{wide}<td class="r">{_num(s["assets"])}</td></tr>')
    rows.append(f'<tr class="grp"><td colspan="{span}">Liabilities</td></tr>')
    if d["liabilities"]:
        rows += [bs_row("Liabilities", _short(r["account"]), r, _natural("Liabilities", r["amount"]))
                 for r in d["liabilities"]]
    else:
        rows.append(f'<tr><td class="muted prose">No liabilities at {_day(end)}.</td>{wide}'
                    f'<td class="r">{_num(0)}</td></tr>')
    rows.append(f'<tr class="sub"><td>Total liabilities</td>{wide}'
                f'<td class="r">{_num(s["liabilities"])}</td></tr>')
    rows.append(f'<tr class="grp"><td colspan="{span}">Equity</td></tr>')
    entries = _entries(d)
    tx_fx = any(p["units_currency"] != c for e in entries for p in e["postings"])
    notes: list[str] = []
    if fx or tx_fx:
        notes.append(
            f"<b>Currency.</b> {esc(d['title'])} keeps its books in {esc(c)}. Balances held in "
            f"another currency are valued at the rate on {_day(end)}; income and expenses stay "
            "at the rate of each transaction's date. Rates are the prices recorded in the book.")
    for r in d["equity"]:
        derived = ":" not in r["account"]
        sup = ""
        if derived:
            notes.append(
                f'<b>{esc(r["account"])}</b> is the profit the book recorded before this '
                "period, each amount at the rate of its own date.")
            sup = f"<sup>{len(notes)}</sup>"
        rows.append(bs_row("Equity", r["account"] if derived else _short(r["account"]),
                           None if derived else r, _natural("Equity", r["amount"]), sup))
    rows.append(f'<tr><td class="acct">Profit for the period</td>{wide}'
                f'<td class="r">{_num(s["profit"])}</td></tr>')
    rows.append(f'<tr class="sub"><td>Total equity</td>{wide}<td class="r">{_num(s["equity"])}</td></tr>')
    rows.append(f'<tr class="tot"><td>Total liabilities and equity</td>{wide}'
                f'<td class="r">{_num(s["liabilities"] + s["equity"])}</td></tr>')
    bs_head = ("<tr><th>Account</th>"
               + ('<th class="r w">Original amount</th><th class="r w">Rate</th>' if fx else "")
               + f'<th class="r">{_day(end)}</th></tr>')

    notice = ""
    if d["unconverted"]:
        items = "".join(
            f'<li><span class="mono">{esc(u["account"])}</span> '
            + _num(_natural(u["account"].split(":", 1)[0], u["amount"]), unit=u["currency"])
            + "</li>" for u in d["unconverted"])
        codes = ", ".join(sorted({u["currency"] for u in d["unconverted"]}))
        notice = (
            f'<div class="notice">{_status("idle", 8)}<span class="eyebrow">Not converted</span>'
            f"<div><p>These amounts are shown in {esc(codes)} and left out of the {esc(c)} "
            f"totals — the book has no {esc(c)} rate for them on the date that applies. Add a "
            f"rate and the report is recalculated.</p><ul>{items}</ul></div></div>")
    if s["gap"] == 0:
        check = (f'<div class="check">{_status("balanced", 8)}<span><strong>Balanced</strong>'
                 " — assets equal liabilities plus equity.</span></div>")
    else:
        check = (
            f'<div class="check">{_status("idle", 8)}<span><strong>Out by '
            f'{abs(s["gap"]):,.2f} {esc(c)}</strong> — balances held in another currency are '
            f"valued at the rate on {_day(end)}, and income at the rate of its own date; the "
            "difference is not posted as a line yet.</span></div>")
    bs = (
        f'<section class="s" aria-labelledby="bs"><div class="sec-h"><h2 id="bs">Balance sheet</h2>'
        f'<span class="sub">at {end} · {esc(c)}</span></div>{notice}'
        f'<div class="scroll"><table class="ledger"><thead>{bs_head}</thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>{check}</section>'
    )

    # --- income statement
    def pnl_rows(root: str) -> str:
        out = []
        for r in d[root.lower()]:
            nc = "".join(
                f'<span class="om on">{esc(_foreign(_flip(root, o["amount"]), o["currency"]))} · no rate, '
                "not in the totals</span>" for o in r["unconverted"])
            out.append(f'<tr><td class="acct">{_acct(_short(r["account"]))}{nc}</td>'
                       f'<td class="r">{_num(_natural(root, r["amount"]))}</td></tr>')
        if not out:
            out.append('<tr><td class="muted prose">No entries yet</td>'
                       f'<td class="r">{_num(0)}</td></tr>')
        return "".join(out)

    is_ = (
        f'<section class="s" aria-labelledby="is"><div class="sec-h"><h2 id="is">Income statement</h2>'
        f'<span class="sub">{start} – {end} · {esc(c)}</span></div>'
        f'<div class="scroll"><table class="ledger"><thead><tr><th>Account</th>'
        f'<th class="r">{esc(s["column"])}</th></tr></thead><tbody>'
        '<tr class="grp"><td colspan="2">Income</td></tr>' + pnl_rows("Income")
        + f'<tr class="sub"><td>Total income</td><td class="r">{_num(s["income"])}</td></tr>'
        '<tr class="grp"><td colspan="2">Expenses</td></tr>' + pnl_rows("Expenses")
        + f'<tr class="sub"><td>Total expenses</td><td class="r">{_num(s["expenses"])}</td></tr>'
        '<tr class="gap"><td colspan="2"></td></tr>'
        f'<tr class="tot"><td>Profit for the period</td>'
        f'<td class="r">{_num(s["profit"], signed=True)}</td></tr>'
        "</tbody></table></div></section>"
    )

    # --- to check
    flagged = s["flagged"]
    if flagged:
        items = []
        for e in flagged:
            main = next((p for p in e["postings"] if p["amount"] > 0), e["postings"][0])
            amt = (_num(main["amount"], unit=c) if main["currency"] == c
                   else _num(main["units"], unit=main["units_currency"]))
            items.append(
                f'<li><span class="st">{_status("refused", 8)}<span class="lab">! Flagged</span></span>'
                f'<span class="dt">{e["date"]}</span><span class="what"><b>'
                f'{esc(e["payee"] or e["narration"])}</b> <span class="acc2">{_acct(main["account"])}</span>'
                + (f'<span class="q">{esc(e["narration"])}</span>' if e["payee"] else "")
                + f'</span><span class="amt">{amt}</span></li>')
        flag_body = f'<ul class="flags">{"".join(items)}</ul>'
        notes.append("<b>Flagged entries</b> are recorded and counted in every total above. "
                     "Each one waits for an answer; the answer moves it, dated in the period "
                     "it belongs to.")
    else:
        flag_body = f'<p class="plain">Nothing was flagged in {esc(s["title"])}.</p>'
    n = len(flagged)
    to_check = (
        f'<section class="s" aria-labelledby="fl"><div class="sec-h"><h2 id="fl">To check</h2>'
        f'<span class="sub">{n} flagged {"entry" if n == 1 else "entries"}</span></div>'
        f"{flag_body}</section>"
    )

    # --- transactions
    jrows = []
    for e in entries:
        flag = (f'<span class="flag">{_status("refused", 8)}! Flagged</span>'
                if e["flag"] == "!" else "")
        title = esc(e["payee"] or e["narration"]) or "—"
        rest = f" — {esc(e['narration'])}" if e["payee"] and e["narration"] else ""
        jrows.append(
            f'<tr class="head"><td class="date w">{e["date"]}</td>'
            f'<td colspan="{4 if tx_fx else 3}"><span class="dm">{e["date"]} · </span>{flag}'
            f'<span class="payee">{title}</span>{rest}</td></tr>')
        for i, p in enumerate(e["postings"]):
            last = " last" if i == len(e["postings"]) - 1 else ""
            if p["currency"] != c:  # no rate on the day: no figure in the report currency
                dr, cr = _dash("no rate on this date"), ""
            else:
                v = _dec(p["amount"])
                dr = _num(v) if v > 0 else ""
                cr = _num(-v) if v < 0 else ""
            orig = om = ""
            if p["units_currency"] != c:
                orig = _num(abs(_dec(p["units"])), unit=p["units_currency"])
                om = f'<span class="om">{esc(_foreign(p["units"], p["units_currency"]))}</span>'
            ocell = f'<td class="r w">{orig}</td>' if tx_fx else ""
            jrows.append(
                f'<tr class="post{last}"><td class="w"></td><td class="acct">{_acct(p["account"])}{om}</td>'
                f'{ocell}<td class="r{" dr" if dr else ""}">{dr}</td>'
                f'<td class="r{" cr" if cr else ""}">{cr}</td></tr>')
    if not entries:
        jrows.append(f'<tr><td colspan="{5 if tx_fx else 4}" class="muted prose">'
                     f"No entries in {esc(s['title'])}.</td></tr>")
    jhead = ('<tr><th class="w">Date</th><th>Entry</th>'
             + ('<th class="r w">Original</th>' if tx_fx else "")
             + '<th class="r">Debit</th><th class="r">Credit</th></tr>')
    tally = ""
    if d.get("transactions_cut"):
        tally = (f'<div class="tally"><span>Showing the latest {len(entries)} entries of the '
                 "period; the earlier ones are in the book.</span></div>")
    txs = (
        f'<section class="s" aria-labelledby="tx"><div class="sec-h"><h2 id="tx">Transactions</h2>'
        f'<span class="sub">{len(entries)} {"entry" if len(entries) == 1 else "entries"} · '
        f"{start} – {end}</span></div>"
        f'<div class="scroll"><table class="ledger journal"><thead>{jhead}</thead>'
        f'<tbody>{"".join(jrows)}</tbody></table></div>{tally}</section>'
    )

    # --- notes
    notes_html = ""
    if notes:
        notes_html = (
            '<section class="s" aria-labelledby="nt"><div class="sec-h"><h2 id="nt">Notes</h2></div>'
            '<ol class="notes">' + "".join(f"<li><p>{n}</p></li>" for n in notes) + "</ol></section>")

    # --- preparation and review
    review = (
        '<section class="s" aria-labelledby="rv"><div class="sec-h"><h2 id="rv">Preparation and '
        'review</h2></div><div class="review"><div><span class="eyebrow">Prepared by</span>'
        '<div class="who">finkpr</div>'
        + (f'<div class="ln">from version <b>{esc(version)}</b> of the book</div>' if version else "")
        + f'<div class="ln">prepared <b>{esc(s["prepared"])}</b></div>'
        "<p>Every entry balanced before it was recorded.</p></div>"
        '<div><span class="eyebrow">Reviewed by</span><div class="who none">Not yet reviewed</div>'
        '<div class="act"><button class="btn btn--secondary" type="button" disabled>Ask your CPA '
        'to review</button> <span class="chip"><span class="period period--refused p8" '
        'aria-hidden="true"></span>NOT BUILT</span><p class="why">Partner-CPA review is not built '
        "yet. Today, send this report or the spreadsheet to your accountant.</p></div></div>"
        "</div></section>"
    )

    body = letterhead + head + kpis + chart + bs + is_ + to_check + txs + notes_html + review + foot
    return _page(f'{d["title"]} — {s["title"]}', body)


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light">'
        f"<title>{html.escape(title)}</title><style>\n{_font_faces()}\n{_root_css()}\n"
        f"{_CSS}</style></head><body>"
        f'<svg width="0" height="0" style="position:absolute" aria-hidden="true">'
        f"{WORDMARK_SYMBOL}</svg>"
        f'<main class="page">{body}</main></body></html>\n'
    )


# The page's sheet. Every colour is a role from :root (brand_tokens.LIGHT);
# radius only 2px (buttons) and 999px (the status period).
_CSS = r"""
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--ground); color: var(--text); font: 400 16px/1.6 var(--body);
       -webkit-font-smoothing: antialiased; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.page { max-width: 1040px; margin: 0 auto; padding: 56px 24px 72px; }
.n, .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.n.pos { color: var(--ledger-pos); } .n.neg { color: var(--ledger-neg); } .n.zero { color: var(--ledger-zero); }
.ccy { font-family: var(--mono); color: var(--text-muted); }
.eyebrow, .lab { font: 500 11px/1.3 var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--text-muted); }
.status { display: inline-flex; align-items: center; gap: 8px; }
.period { display: inline-block; border-radius: 999px; flex: none; }
.p8 { width: 8px; height: 8px; } .p12 { width: 12px; height: 12px; }
.period--balanced { background: var(--ledger-pos); }
.period--refused { background: var(--ledger-neg); }
.period--idle { background: var(--text-muted); }
.chip { display: inline-flex; align-items: center; gap: 6px; font: 500 11px/1.3 var(--mono); letter-spacing: .14em; color: var(--text-muted); white-space: nowrap; }
.btn { display: inline-flex; align-items: center; justify-content: center; min-height: var(--target); padding: 0 20px;
       font: 600 15px/1 var(--body); border-radius: var(--radius); border: 1px solid transparent; }
.btn[disabled] { background: var(--ground); color: var(--text-muted); border: 1px dashed var(--rule-strong); cursor: not-allowed; }
.why { font: 400 13px/1.55 var(--body); color: var(--text-muted); margin: 6px 0 0; }
.wm { display: block; align-self: flex-start; height: auto; aspect-ratio: 3365 / 1133; }
.wm-l { fill: var(--mark-letters); } .wm-p { fill: var(--mark-period); }
.letterhead { display: flex; justify-content: space-between; align-items: flex-start; gap: 24px;
              padding-bottom: 24px; border-bottom: var(--close) solid var(--rule-close); }
.meta { display: grid; grid-template-columns: auto auto; gap: 4px 16px; margin: 0; text-align: right; }
.meta dt { font: 500 11px/1.9 var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--text-muted); }
.meta dd { margin: 0; font: 400 14px/1.4 var(--mono); color: var(--text); padding-top: 1px; }
.title { padding: 56px 0 40px; }
h1 { font: 700 46px/1.2 var(--display); letter-spacing: -.03em; margin: 12px 0 16px; }
h1 .acc { color: var(--interactive); }
.statusline { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font: 400 16px/1.5 var(--body); }
.statusline .pword { font-weight: 600; }
.statusline .meta2 { font: 400 14px/1.4 var(--mono); color: var(--text-muted); }
.lead { font: 400 18px/1.6 var(--body); max-width: 65ch; margin: 16px 0 0; }
.kpis { display: grid; grid-template-columns: repeat(var(--k), 1fr); border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule); }
.kpi { padding: 24px; border-left: 1px solid var(--rule); }
.kpi:first-child { border-left: 0; padding-left: 0; }
.kpi .fig { font: 500 32px/1.3 var(--mono); margin: 12px 0 4px; white-space: nowrap; }
.kpi .sub { font: 400 14px/1.4 var(--mono); color: var(--text-muted); }
.charts { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; padding: 40px 0 0; }
.hbars { margin-top: 24px; display: grid; gap: 12px; }
.hb { display: grid; grid-template-columns: 180px 1fr 110px; align-items: center; gap: 12px; }
.hb .a { font: 400 14px/1.4 var(--mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hb .t i { display: block; height: 16px; background: var(--t5); }
.hb .n { text-align: right; font-size: 14px; }
section.s { padding-top: 72px; }
.sec-h { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }
h2 { font: 700 32px/1.25 var(--display); letter-spacing: -.02em; margin: 0; }
.sec-h .sub { font: 400 14px/1.4 var(--mono); color: var(--text-muted); }
.scroll { overflow-x: auto; }
table.ledger { width: 100%; border-collapse: collapse; font: 400 16px/1.45 var(--mono); font-variant-numeric: tabular-nums; }
.ledger th { font: 500 11px/1.3 var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--text-muted);
             text-align: left; padding: 0 0 8px; border-bottom: 1px solid var(--rule); white-space: nowrap; }
.ledger th.r, .ledger td.r { text-align: right; padding-left: 24px; }
.ledger th.r { padding-right: 1ch; }
.ledger td { padding: 10px 0; border-bottom: 1px solid var(--rule); vertical-align: baseline; }
.ledger td.r { white-space: nowrap; }
.ledger td.r .n { display: inline-block; margin-right: 1ch; } .ledger td.r .n.paren { margin-right: 0; }
.ledger td.acct { padding-right: 16px; }
.ledger .muted { color: var(--text-muted); }
.ledger .prose { font-family: var(--body); font-size: 15px; }
.ledger tr.grp td { font: 500 11px/1.3 var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--text-muted); padding: 24px 0 8px; }
.ledger tr.grp:first-child td { padding-top: 8px; }
.ledger tr.sub td { border-top: 1px solid var(--rule-close); font-weight: 500; }
.ledger tr.tot td { border-top: var(--close) solid var(--rule-close); border-bottom: 0; font-weight: 500; font-size: 18px; padding-top: 12px; }
.ledger tr.gap td { border: 0; padding: 12px 0 0; }
.ledger sup { font: 500 11px/1 var(--mono); color: var(--text-muted); margin-left: 4px; }
.ledger td .ccy { font-size: 14px; }
.om { display: none; }
.om.on { display: block; font: 400 13px/1.4 var(--mono); color: var(--text-muted); margin-top: 2px; }
.check { display: flex; align-items: baseline; gap: 10px; margin-top: 16px; font: 400 15px/1.5 var(--body); color: var(--text-muted); max-width: 75ch; }
.check strong { color: var(--text); font-weight: 600; }
.notice { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; border-left: 1px solid var(--rule-strong);
          padding: 4px 0 4px 16px; margin: 0 0 24px; max-width: 75ch; }
.notice .status { grid-row: span 2; align-self: start; padding-top: 5px; }
.notice p { margin: 0; font: 400 15px/1.55 var(--body); }
.notice ul { margin: 8px 0 0; padding-left: 18px; font-size: 14px; }
.plain { font: 400 16px/1.6 var(--body); margin: 0; }
.flags { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--rule); }
.flags li { display: grid; grid-template-columns: 130px 110px 1fr auto; gap: 4px 16px; align-items: baseline; padding: 14px 0; border-bottom: 1px solid var(--rule); }
.flags .st { display: inline-flex; align-items: center; gap: 8px; }
.flags .st .lab { color: var(--text); }
.flags .dt { font: 400 14px/1.5 var(--mono); color: var(--text-muted); }
.flags .what { font: 400 16px/1.5 var(--body); }
.flags .what b { font-weight: 600; }
.flags .acc2 { font: 400 14px/1.5 var(--mono); }
.flags .q { display: block; font: 400 14px/1.5 var(--body); color: var(--text-muted); }
.flags .amt { font: 400 16px/1.5 var(--mono); text-align: right; white-space: nowrap; }
.dm { display: none; }
.journal td.date { width: 120px; white-space: nowrap; color: var(--text-muted); font-size: 14px; }
.journal tr.head td { border-bottom: 0; padding-bottom: 2px; font: 400 16px/1.5 var(--body); }
.journal tr.head td.date { font: 400 14px/1.5 var(--mono); color: var(--text-muted); }
.journal tr.head .payee { font-weight: 600; }
.journal tr.post td { border-bottom: 0; padding: 2px 0; font-size: 15px; }
.journal tr.post td.acct { padding-left: 24px; }
.journal tr.post.last td { border-bottom: 1px solid var(--rule); padding-bottom: 12px; }
.journal .flag { display: inline-flex; align-items: center; gap: 6px; margin-right: 8px; font: 500 11px/1 var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--text-muted); }
.tally { padding-top: 16px; font: 400 15px/1.5 var(--body); color: var(--text-muted); }
.notes { list-style: none; counter-reset: n; margin: 0; padding: 0; max-width: 75ch; }
.notes li { counter-increment: n; display: grid; grid-template-columns: 32px 1fr; padding: 12px 0; border-bottom: 1px solid var(--rule); font: 400 15px/1.6 var(--body); }
.notes li::before { content: counter(n); font: 500 14px/1.7 var(--mono); color: var(--text-muted); }
.notes li p { margin: 0; }
.review { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; border-top: 1px solid var(--rule); padding-top: 24px; }
.review .who { font: 600 18px/1.4 var(--body); margin: 12px 0 4px; }
.review .who.none { color: var(--text-muted); font-weight: 400; }
.review .ln { font: 400 14px/1.6 var(--mono); color: var(--text-muted); }
.review .ln b { color: var(--text); font-weight: 500; }
.review p { font: 400 15px/1.55 var(--body); margin: 12px 0 0; max-width: 46ch; }
.review .act { margin-top: 16px; }
.review .act p.why { font-size: 13px; }
footer.foot { margin-top: 72px; padding-top: 16px; border-top: 1px solid var(--rule); display: flex; justify-content: space-between; gap: 24px; flex-wrap: wrap; }
footer.foot .l { font: 400 14px/1.6 var(--mono); color: var(--text-muted); }
footer.foot p { margin: 0; font: 400 13px/1.5 var(--body); color: var(--text-muted); max-width: 60ch; }
.empty-state { padding: 24px 0 0; }
.empty-state p { font: 400 18px/1.6 var(--body); margin: 0; max-width: 60ch; }
.empty-state p.why { font-size: 15px; margin-top: 12px; }
/* phone and the Telegram in-app viewer: compact grid, 16px gutter */
@media (max-width: 699px) {
  .page { padding: 24px 16px 48px; }
  .letterhead { flex-direction: column; gap: 16px; }
  .meta { text-align: left; grid-template-columns: 96px 1fr; }
  .title { padding: 32px 0 24px; }
  h1 { font-size: 32px; letter-spacing: -.02em; }
  .lead { font-size: 16px; }
  .kpis { grid-template-columns: 1fr 1fr; }
  .kpi { padding: 16px 0 16px 16px; }
  .kpi:nth-child(odd) { border-left: 0; padding-left: 0; }
  .kpi:nth-child(n+3) { border-top: 1px solid var(--rule); }
  .kpi .fig { font-size: 20px; white-space: normal; }
  .kpi .sub { font-size: 13px; overflow-wrap: anywhere; }
  .charts { grid-template-columns: 1fr; }
  .hb { grid-template-columns: 110px 1fr 92px; gap: 8px; }
  section.s { padding-top: 48px; }
  h2 { font-size: 26px; }
  table.ledger { font-size: 14px; }
  .ledger .w { display: none; }
  .ledger .om { display: block; font: 400 13px/1.4 var(--mono); color: var(--text-muted); margin-top: 2px; }
  .ledger th.r, .ledger td.r { padding-left: 12px; }
  .ledger td.acct { overflow-wrap: anywhere; }
  .dm { display: inline; }
  .journal tr.post { display: grid; grid-template-columns: 1fr auto; }
  .journal tr.post td:empty, .journal tr.post td.w { display: none; }
  .journal tr.post td.acct { padding-left: 12px; font-size: 14px; }
  .journal tr.post td.dr::before, .journal tr.post td.cr::before { font: 500 11px/1 var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--text-muted); margin-right: 8px; }
  .journal tr.post td.dr::before { content: "Dr"; } .journal tr.post td.cr::before { content: "Cr"; }
  .journal thead { display: none; }
  .flags li { grid-template-columns: auto 1fr; }
  .flags .dt { text-align: right; }
  .flags .what, .flags .amt { grid-column: 1 / -1; text-align: left; }
  .notes li { font-size: 14px; }
  .review { grid-template-columns: 1fr; }
}
@media print {
  .page { padding: 0; max-width: none; }
  tr { break-inside: avoid; }
  @page { size: A4; margin: 18mm 16mm; }
}
"""


# ---------------------------------------------------------------- Excel ------
# Excel has no Work Sans or Plex: text is Arial, figures Consolas (bundled with
# Office on Windows and Mac). Negatives in parentheses and in ink — the old
# `[Red]` format coloured every negative, balance-sheet lines included. Only a
# signed result (profit) takes ledger colour, and it carries its sign.
_XL_TEXT, _XL_FIG = "Arial", "Consolas"
_XL_FMT = '#,##0.00;(#,##0.00);0.00'
_XL_SIGNED = '+#,##0.00;(#,##0.00);0.00'


def generate_xlsx(
    ledger: Ledger, out_path: str, as_of: str | None = None, period_start: str | None = None
) -> str:
    try:
        from openpyxl import Workbook
        from openpyxl.formatting.rule import CellIsRule
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError as e:  # pragma: no cover
        raise LedgerError("openpyxl not installed — `pip install openpyxl`.") from e

    d = collect(ledger, as_of, period_start)
    s = _statement(d)
    c = s["ccy"]
    ink, muted, hair = xl(LIGHT["text"]), xl(LIGHT["text_muted"]), xl(LIGHT["rule"])
    paper, pos, neg = xl(LIGHT["ground"]), xl(LIGHT["ledger_pos"]), xl(LIGHT["ledger_neg"])
    thin = Border(bottom=Side(style="thin", color=hair))
    head_fill = PatternFill("solid", fgColor=ink)
    version = s["version"]
    ver = f" · version {version}" if version else ""
    start, end = d["period_start"], d["as_of"]

    wb = Workbook()
    wb.remove(wb.active)

    def sheet(name: str, sub: str, headers: list[str], widths: list[int], right: set[int]):
        ws = wb.create_sheet(name)
        ws.sheet_view.showGridLines = False
        ws["A1"] = f'{d["title"]} — {name}'
        ws["A1"].font = Font(name=_XL_TEXT, size=14, bold=True, color=ink)
        ws["A2"] = sub
        ws["A2"].font = Font(name=_XL_TEXT, size=11, color=muted)
        for i, h in enumerate(headers, 1):
            cell = ws.cell(4, i, h)
            cell.fill = head_fill
            cell.font = Font(name=_XL_TEXT, size=11, bold=True, color=paper)
            cell.alignment = Alignment(horizontal="right" if i in right else "left",
                                       vertical="center")
        ws.row_dimensions[4].height = 22
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A5"
        ws.page_setup.orientation = "portrait"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.oddFooter.left.text = "finkpr" + ver
        ws.oddFooter.right.text = "Page &P of &N"
        return ws

    def fig(ws, r, col, v, bold=False, fmt=_XL_FMT, border=thin):
        cell = ws.cell(r, col, float(v) if v is not None else None)
        cell.number_format = fmt
        cell.font = Font(name=_XL_FIG, size=11, bold=bold, color=ink)
        cell.alignment = Alignment(horizontal="right")
        if border is not None:
            cell.border = border
        return cell

    def text(ws, r, col, v, mono=False, bold=False, color=None, border=thin):
        cell = ws.cell(r, col, v)
        cell.font = Font(name=_XL_FIG if mono else _XL_TEXT, size=11, bold=bold,
                         color=color or ink)
        if border is not None:
            cell.border = border
        return cell

    def close_above(ws, r, ncols, style):
        # The closing rule over a total: thin ink for a subtotal, medium for a
        # total (the document's 2px rule).
        for col in range(1, ncols + 1):
            ws.cell(r - 1, col).border = Border(bottom=Side(style=style, color=ink))

    def signed_colour(ws, ref):
        ws.conditional_formatting.add(ref, CellIsRule(operator="greaterThan", formula=["0"],
                                                      font=Font(color=pos)))
        ws.conditional_formatting.add(ref, CellIsRule(operator="lessThan", formula=["0"],
                                                      font=Font(color=neg)))

    # ---- Balance sheet
    # Totals are written as VALUES, not =SUM(): openpyxl cannot store a
    # formula's result, and a phone's preview (Telegram, Quick Look) shows a
    # formula it has not computed as an empty cell.
    ws = sheet("Balance sheet",
               f"At {_day(end)} · {c}{ver} · prepared by finkpr {s['prepared'][:10]}",
               ["Account", "Original amount", "Currency", "Rate", _day(end)],
               [42, 18, 10, 10, 18], {2, 4, 5})
    r = 5

    def bs_section(label, root, rows, total_label, total, style, extra=()):
        nonlocal r
        text(ws, r, 1, label, bold=True, border=None)
        r += 1
        for x in rows:
            text(ws, r, 1, x["account"], mono=True)
            held = _held(x, c, root)
            if len(held) == 1:
                fig(ws, r, 2, held[0]["amount"])
                text(ws, r, 3, held[0]["currency"], mono=True)
                if held[0]["rate"] is not None:
                    fig(ws, r, 4, held[0]["rate"], fmt="0.0000")
                else:
                    text(ws, r, 4, "no rate", mono=True, color=muted)
            elif held:
                text(ws, r, 2, ", ".join(_foreign(h["amount"], h["currency"]) for h in held),
                     mono=True)
                ws.cell(r, 3).border = thin
                text(ws, r, 4, "no rate" if any(h["rate"] is None for h in held) else "",
                     mono=True, color=muted)
            else:
                for col in (2, 3, 4):
                    ws.cell(r, col).border = thin
            fig(ws, r, 5, _natural(root, x["amount"]))
            r += 1
        for name, v in extra:
            text(ws, r, 1, name)
            for col in (2, 3, 4):
                ws.cell(r, col).border = thin
            fig(ws, r, 5, v)
            r += 1
        close_above(ws, r, 5, style)
        text(ws, r, 1, total_label, bold=True, border=None)
        fig(ws, r, 5, total, bold=True, border=None)
        r += 2

    bs_section("Assets", "Assets", d["assets"], "Total assets", s["assets"], "medium")
    bs_section("Liabilities", "Liabilities", d["liabilities"], "Total liabilities",
               s["liabilities"], "thin")
    bs_section("Equity", "Equity", d["equity"], "Total equity", s["equity"], "thin",
               extra=[("Profit for the period", s["profit"])])
    close_above(ws, r, 5, "medium")
    text(ws, r, 1, "Total liabilities and equity", bold=True, border=None)
    fig(ws, r, 5, s["liabilities"] + s["equity"], bold=True, border=None)
    r += 2
    lines = []
    if s["foreign"]:
        lines.append("Balances in another currency are valued at the rate on the balance-sheet "
                     "date. Rates are the prices recorded in the book.")
    if s["gap"]:
        lines.append(f'Out by {abs(s["gap"]):,.2f} {c}: the currency difference is not posted '
                     "as a line yet.")
    if any(x["unconverted"] for k in ("assets", "liabilities", "equity") for x in d[k]):
        lines.append(f"An amount marked \"no rate\" has no {c} price on the date that applies "
                     "and is in no total.")
    for line in lines:
        text(ws, r, 1, line, color=muted, border=None)
        r += 1

    # ---- Income statement
    pnl_nc = any(x["unconverted"] for k in ("income", "expenses") for x in d[k])
    heads = ["Account", s["column"]] + (["Not converted"] if pnl_nc else [])
    ws = sheet("Income statement", f"{start} – {end} · {c}{ver}", heads,
               [42, 18] + ([24] if pnl_nc else []), {2})
    r = 5
    for label, root, total in (("Income", "Income", s["income"]),
                               ("Expenses", "Expenses", s["expenses"])):
        text(ws, r, 1, label, bold=True, border=None)
        r += 1
        for x in d[root.lower()]:
            text(ws, r, 1, x["account"], mono=True)
            fig(ws, r, 2, _natural(root, x["amount"]))
            if pnl_nc:
                text(ws, r, 3, ", ".join(_foreign(_flip(root, o["amount"]), o["currency"])
                                         for o in x["unconverted"]), mono=True)
            r += 1
        close_above(ws, r, len(heads), "thin")
        text(ws, r, 1, f"Total {label.lower()}", bold=True, border=None)
        fig(ws, r, 2, total, bold=True, border=None)
        r += 2
    close_above(ws, r, len(heads), "medium")
    text(ws, r, 1, "Profit for the period", bold=True, border=None)
    fig(ws, r, 2, s["profit"], bold=True, fmt=_XL_SIGNED, border=None)
    signed_colour(ws, f"B{r}")

    # ---- Transactions: every posting of the period, debit / credit
    entries = _entries(d)
    ws = sheet("Transactions", f"{start} – {end} · every posting · {c}{ver}",
               ["Date", "Flag", "Payee", "Narration", "Account", "Original", "Currency",
                "Debit", "Credit"],
               [11, 6, 22, 32, 38, 14, 9, 14, 14], {6, 8, 9})
    r = 5
    for e in entries:
        for p in e["postings"]:
            text(ws, r, 1, e["date"], mono=True)
            text(ws, r, 2, "!" if e["flag"] == "!" else "", mono=True)
            text(ws, r, 3, e["payee"])
            text(ws, r, 4, e["narration"])
            text(ws, r, 5, p["account"], mono=True)
            if p["units_currency"] != c:
                fig(ws, r, 6, abs(_dec(p["units"])))
                text(ws, r, 7, p["units_currency"], mono=True)
            else:
                ws.cell(r, 6).border = ws.cell(r, 7).border = thin
            v = _dec(p["amount"]) if p["currency"] == c else None
            if v is None:
                text(ws, r, 8, "no rate", mono=True, color=muted)
                ws.cell(r, 9).border = thin
            else:
                fig(ws, r, 8, v if v > 0 else None)
                fig(ws, r, 9, -v if v < 0 else None)
            r += 1
    if d.get("transactions_cut"):
        text(ws, r + 1, 1, f"The latest {len(entries)} entries of the period; the earlier ones "
                           "are in the book.", color=muted, border=None)

    # ---- To check: the entries flagged `!`, each waiting for an answer
    ws = sheet("To check", f"{start} – {end} · flagged entries · {c}{ver}",
               ["Date", "Payee", "Narration", "Account", "Amount", "Currency"],
               [11, 22, 36, 38, 14, 9], {5})
    r = 5
    for e in s["flagged"]:
        main = next((p for p in e["postings"] if p["amount"] > 0), e["postings"][0])
        text(ws, r, 1, e["date"], mono=True)
        text(ws, r, 2, e["payee"])
        text(ws, r, 3, e["narration"])
        text(ws, r, 4, main["account"], mono=True)
        if main["currency"] == c:
            fig(ws, r, 5, main["amount"])
            text(ws, r, 6, c, mono=True)
        else:
            fig(ws, r, 5, main["units"])
            text(ws, r, 6, main["units_currency"], mono=True)
        r += 1
    if not s["flagged"]:
        text(ws, r, 1, f'Nothing was flagged in {s["title"]}.', color=muted, border=None)

    wb.save(out_path)
    return out_path
