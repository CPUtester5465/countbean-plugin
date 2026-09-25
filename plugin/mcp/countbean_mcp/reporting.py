"""Report generation for a Countbean book: styled HTML and Excel (.xlsx).

Both formats are built from the same numbers (``collect``) so they always agree.

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

from .ledger import Ledger, LedgerError

# Root names the report sections by. Beancount lets a book rename them
# (`option "name_assets"`); no Countbean book does, and the templates assume these.
BALANCE_ROOTS = ("Assets", "Liabilities", "Equity")
PNL_ROOTS = ("Income", "Expenses")

CENT = Decimal("0.01")

# How many journal lines the report shows. Unchanged from the CSV version.
TXN_LIMIT = 100


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
    balance = run(
        f"SELECT account, sum(convert(position, '{ccy}', {as_of_d})) AS balance "
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
    txns = run(
        f"SELECT date, payee, narration, account, convert(position, '{ccy}', date) AS amount "
        f"WHERE date <= {as_of_d} ORDER BY date DESC, narration LIMIT {TXN_LIMIT}"
    )
    return {
        "currency": ccy,
        "balance": [[acct, positions(inv)] for acct, inv in balance],
        "pnl": [[acct, positions(inv)] for acct, inv in pnl],
        # An aggregate over no rows is still one row, holding None or an empty
        # inventory; `positions` makes both [].
        "retained": positions(retained[0][0]) if retained else [],
        "transactions": [
            [d.isoformat(), payee or "", narration or "", acct,
             str(amt.number) if amt is not None else "0",
             amt.currency if amt is not None else ccy]
            for d, payee, narration, acct, amt in txns
        ],
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
    # Profit (or loss) from before the period, as Equity — the sign is the
    # book's own (a profit is a credit, negative, like Equity:Opening-Balances
    # above it). Omitted when there is none, e.g. a book opened this year.
    if raw["retained"]:
        add("Equity", f"Retained earnings (before {start})", raw["retained"])

    txns = [
        {"date": d, "payee": payee, "narration": narration, "account": acct,
         "amount": _money(Decimal(number)), "currency": currency}
        for d, payee, narration, acct, number, currency in raw["transactions"]
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
    return "Countbean"


# ---------------------------------------------------------------- HTML -------
def _foreign(amount: float, currency: str) -> str:
    # Enough places for the amount as booked — 0.12345678 BTC must not print
    # as 0.12 — and always the currency code beside it, because a bare 4,000.00
    # in a dollar report reads as dollars.
    return f"{amount:,.{max(2, -Decimal(str(amount)).as_tuple().exponent)}f} {currency}"


def generate_html(
    ledger: Ledger, out_path: str, as_of: str | None = None, period_start: str | None = None
) -> str:
    d = collect(ledger, as_of, period_start)
    c = d["currency"]

    def money(n: float) -> str:
        sign = "neg" if n < 0 else "pos" if n > 0 else "zero"
        return f'<span class="m {sign}">{n:,.2f}</span>'

    def not_converted(x: dict) -> str:
        if not x["unconverted"]:
            return ""
        parts = ", ".join(_foreign(o["amount"], o["currency"]) for o in x["unconverted"])
        return f'<div class="nc">+ {html.escape(parts)} &middot; not in {c}</div>'

    def rows(section: list[dict]) -> str:
        if not section:
            return '<tr><td class="acct empty" colspan="2">No entries yet</td></tr>'
        return "".join(
            f'<tr><td class="acct">{html.escape(x["account"])}{not_converted(x)}</td>'
            f'<td class="num">{money(x["amount"])}</td></tr>'
            for x in section
        )

    def notice() -> str:
        if not d["unconverted"]:
            return ""
        items = "".join(
            f'<li><span class="acct">{html.escape(u["account"])}</span> '
            f'{html.escape(_foreign(u["amount"], u["currency"]))}</li>'
            for u in d["unconverted"]
        )
        return (
            f'<div class="notice"><strong>Not converted to {c}.</strong> These amounts '
            f'have no {c} price on the date that applies, so they are shown in their '
            f'own currency and left out of every total on this page. Add a price '
            f'directive to include them.<ul>{items}</ul></div>'
        )

    def amount_cell(t: dict) -> str:
        if t["currency"] == c:
            return money(t["amount"])
        return f'<span class="m nc">{html.escape(_foreign(t["amount"], t["currency"]))}</span>'

    def txn_rows() -> str:
        if not d["transactions"]:
            return '<tr><td class="empty" colspan="5">No transactions yet</td></tr>'
        out = []
        for t in d["transactions"]:
            out.append(
                "<tr>"
                f'<td class="date">{html.escape(t["date"])}</td>'
                f'<td>{html.escape(t["payee"])}</td>'
                f'<td>{html.escape(t["narration"])}</td>'
                f'<td class="acct">{html.escape(t["account"])}</td>'
                f'<td class="num">{amount_cell(t)}</td>'
                "</tr>"
            )
        return "".join(out)

    t = d["totals"]
    doc = _HTML_TEMPLATE.format(
        title=html.escape(d["title"]),
        as_of=d["as_of"],
        period_start=d["period_start"],
        generated=d["generated"],
        ccy=c,
        notice=notice(),
        net_worth=money(t["net_worth"]),
        net_income=money(t["net_income"]),
        assets_total=money(t["assets"]),
        liabilities_total=money(t["liabilities"]),
        assets_rows=rows(d["assets"]),
        liabilities_rows=rows(d["liabilities"]),
        equity_rows=rows(d["equity"]),
        income_rows=rows(d["income"]),
        expenses_rows=rows(d["expenses"]),
        income_total=money(t["income"]),
        expenses_total=money(t["expenses"]),
        txn_rows=txn_rows(),
    )
    Path(out_path).write_text(doc)
    return out_path


# ---------------------------------------------------------------- Excel ------
def generate_xlsx(
    ledger: Ledger, out_path: str, as_of: str | None = None, period_start: str | None = None
) -> str:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError as e:  # pragma: no cover
        raise LedgerError("openpyxl not installed — `pip install openpyxl`.") from e

    d = collect(ledger, as_of, period_start)
    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="146B4A")
    head_font = Font(bold=True, color="FFFFFF")
    title_font = Font(bold=True, size=14, color="146B4A")
    money_fmt = '#,##0.00;[Red]-#,##0.00'

    def sheet(name, subtitle, sections):
        ws = wb.create_sheet(name)
        ws["A1"] = f'{d["title"]} — {name}'
        ws["A1"].font = title_font
        ws["A2"] = f'{subtitle}  ·  {d["currency"]}'
        ws["A2"].font = Font(italic=True, color="6D7D74")
        r = 4
        for label, rows_ in sections:
            ws.cell(r, 1, label).font = Font(bold=True)
            r += 1
            # Column C exists so an amount with no price has somewhere to be
            # that is NOT column B: the old sheet put 4,000 GBP there as dollars.
            for col, h in enumerate(("Account", "Balance", "Not converted"), 1):
                ws.cell(r, col, h).font = head_font
                ws.cell(r, col).fill = head_fill
            r += 1
            subtotal = Decimal(0)
            for x in rows_:
                ws.cell(r, 1, x["account"])
                cell = ws.cell(r, 2, x["amount"])
                cell.number_format = money_fmt
                cell.alignment = Alignment(horizontal="right")
                if x["unconverted"]:
                    ws.cell(r, 3, ", ".join(
                        _foreign(o["amount"], o["currency"]) for o in x["unconverted"]))
                subtotal += Decimal(str(x["amount"]))
                r += 1
            ws.cell(r, 1, f"Total {label}").font = Font(bold=True)
            tc = ws.cell(r, 2, float(subtotal))
            tc.font = Font(bold=True)
            tc.number_format = money_fmt
            r += 2
        if d["unconverted"]:
            ws.cell(r, 1, f'Amounts under "Not converted" have no {d["currency"]} price on '
                          f"the date that applies and are in no total.").font = Font(
                italic=True, color="B04A2F")
        ws.column_dimensions["A"].width = 38
        ws.column_dimensions["B"].width = 18
        ws.column_dimensions["C"].width = 24
        return ws

    sheet("Balance Sheet", f'As of {d["as_of"]}',
          [("Assets", d["assets"]), ("Liabilities", d["liabilities"]), ("Equity", d["equity"])])
    sheet("Income Statement", f'{d["period_start"]} to {d["as_of"]}',
          [("Income", d["income"]), ("Expenses", d["expenses"])])

    ws = wb.create_sheet("Transactions")
    headers = ["Date", "Payee", "Narration", "Account", "Amount", "Currency"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(1, col, h)
        cell.font = head_font
        cell.fill = head_fill
    for i, tr in enumerate(d["transactions"], start=2):
        ws.cell(i, 1, tr["date"])
        ws.cell(i, 2, tr["payee"])
        ws.cell(i, 3, tr["narration"])
        ws.cell(i, 4, tr["account"])
        c = ws.cell(i, 5, tr["amount"])
        c.number_format = money_fmt
        ws.cell(i, 6, tr["currency"])
    widths = [12, 22, 30, 34, 14, 10]
    for col, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + col)].width = w

    wb.remove(wb["Sheet"])  # drop default
    wb.save(out_path)
    return out_path


_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Report</title>
<style>
  :root {{
    --paper:#eef1ea; --card:#f7f9f3; --ink:#16211a; --soft:#40524a; --faint:#6d7d74;
    --line:#cdd6c8; --pine:#146b4a; --pos:#147a52; --neg:#b04a2f;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
    --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }}
  @media (prefers-color-scheme:dark) {{
    :root {{ --paper:#0e1512; --card:#14201a; --ink:#e7ede4; --soft:#b3c1b6; --faint:#7d8f83;
      --line:#26332b; --pine:#2fb37c; --pos:#43c088; --neg:#e08363; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:var(--sans); background:var(--paper); color:var(--ink);
    line-height:1.55; padding:clamp(20px,5vw,56px); }}
  .wrap {{ max-width:900px; margin:0 auto; }}
  header {{ display:flex; justify-content:space-between; align-items:flex-end; flex-wrap:wrap;
    gap:12px; border-bottom:2px solid var(--pine); padding-bottom:16px; margin-bottom:8px; }}
  h1 {{ font-family:var(--mono); font-size:clamp(22px,3vw,30px); margin:0; letter-spacing:-.02em; }}
  .meta {{ font-family:var(--mono); font-size:12.5px; color:var(--faint); text-align:right; }}
  .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:14px;
    margin:26px 0; }}
  .kpi {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:16px 18px; }}
  .kpi .l {{ font-family:var(--mono); font-size:11px; letter-spacing:.1em; text-transform:uppercase;
    color:var(--faint); }}
  .kpi .v {{ font-family:var(--mono); font-size:26px; font-variant-numeric:tabular-nums; margin-top:6px; }}
  h2 {{ font-family:var(--mono); font-size:15px; letter-spacing:.05em; text-transform:uppercase;
    color:var(--pine); margin:34px 0 12px; }}
  .cols {{ display:grid; grid-template-columns:1fr 1fr; gap:22px; }}
  @media (max-width:640px) {{ .cols {{ grid-template-columns:1fr; }} }}
  table {{ width:100%; border-collapse:collapse; background:var(--card);
    border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
  caption {{ text-align:left; font-family:var(--mono); font-size:12px; color:var(--faint);
    padding:10px 12px; background:transparent; }}
  td, th {{ padding:8px 12px; border-bottom:1px solid var(--line); text-align:left; font-size:14px; }}
  tr:last-child td {{ border-bottom:none; }}
  .acct {{ font-family:var(--mono); font-size:13px; }}
  .num, .m {{ font-family:var(--mono); text-align:right; font-variant-numeric:tabular-nums; }}
  td.num {{ text-align:right; }}
  .m.pos {{ color:var(--pos); }} .m.neg {{ color:var(--neg); }} .m.zero {{ color:var(--faint); }}
  .total td {{ font-weight:700; border-top:2px solid var(--line); }}
  .empty {{ color:var(--faint); font-style:italic; }}
  .nc {{ font-family:var(--mono); font-size:12px; color:var(--neg); }}
  .notice {{ background:var(--card); border:1px solid var(--neg); border-radius:8px;
    padding:12px 16px; margin:18px 0; font-size:14px; }}
  .notice ul {{ margin:8px 0 0; padding-left:20px; }}
  .scroll {{ overflow-x:auto; }}
  footer {{ margin-top:40px; font-family:var(--mono); font-size:11.5px; color:var(--faint);
    text-align:center; }}
</style></head>
<body><div class="wrap">
  <header>
    <h1>{title}</h1>
    <div class="meta">Report as of {as_of}<br>Income {period_start} to {as_of}<br>Generated {generated} · {ccy}</div>
  </header>
{notice}
  <div class="kpis">
    <div class="kpi"><div class="l">Net worth</div><div class="v">{net_worth}</div></div>
    <div class="kpi"><div class="l">Assets</div><div class="v">{assets_total}</div></div>
    <div class="kpi"><div class="l">Liabilities</div><div class="v">{liabilities_total}</div></div>
    <div class="kpi"><div class="l">Net income since {period_start}</div><div class="v">{net_income}</div></div>
  </div>

  <h2>Balance Sheet</h2>
  <div class="cols">
    <table><caption>Assets</caption>{assets_rows}</table>
    <table><caption>Liabilities &amp; Equity</caption>{liabilities_rows}{equity_rows}</table>
  </div>

  <h2>Income Statement · {period_start} to {as_of}</h2>
  <div class="cols">
    <table><caption>Income</caption>{income_rows}
      <tr class="total"><td class="acct">Total income</td><td class="num">{income_total}</td></tr></table>
    <table><caption>Expenses</caption>{expenses_rows}
      <tr class="total"><td class="acct">Total expenses</td><td class="num">{expenses_total}</td></tr></table>
  </div>

  <h2>Recent Transactions</h2>
  <div class="scroll"><table>
    <tr><th>Date</th><th>Payee</th><th>Narration</th><th>Account</th><th class="num">Amount</th></tr>
    {txn_rows}
  </table></div>

  <footer>Generated by Countbean · plain-text accounting, maintained by AI</footer>
</div></body></html>
"""
