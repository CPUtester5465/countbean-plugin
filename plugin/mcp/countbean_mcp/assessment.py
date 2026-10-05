"""Facts about a book, computed — so that Claude reports rather than guesses.

WHY THIS FILE IS SHAPED THE WAY IT IS
-------------------------------------
"Have Claude review my books and tell me what it sees" is the feature. The
danger in it is specific and it is not hypothetical: a language model handed a
ledger will produce a confident number. "Your burn rate is $4,200/month" off
three weeks of data. "Spending on software is up 40%" when the comparison month
is half-recorded. "This $9,500 charge looks anomalous" about a rent payment in a
book with four transactions in it.

Each of those is worse than saying nothing, because the customer acts on it, and
because being wrong about somebody's money is the one thing an accounting
product cannot be casual about.

So the split is: THIS FILE DECIDES WHAT IS TRUE, and the model writes prose
about it. Every derived figure carries whether the data supports it and why not
when it does not, and the command that renders this is told to report only what
appears here. A metric that cannot be computed honestly comes back as
`sufficient: false` with a reason a human can read — never as a number with a
caveat attached, because caveats are what get dropped in summarisation.

WHAT IT DOES NOT DO
-------------------
It does not categorise, forecast, or advise. Those are judgements; this is
arithmetic. `run_query` remains available for anything ad hoc.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

# Months of complete data before a run-rate figure means anything. Two months is
# one comparison and no sense of variance; three is the least that can show a
# trend rather than a coincidence.
MIN_MONTHS_FOR_RUN_RATE = 3

# Postings in an account before "unusually large" is a claim rather than an
# observation about a tiny sample. With three data points every one of them is
# both the largest and the smallest.
MIN_SAMPLES_FOR_OUTLIER = 4

# How many times the account's own median an amount must exceed. Per-account on
# purpose: rent is always large and never an anomaly; a 4x grocery bill is one.
OUTLIER_MULTIPLE = Decimal("4")


def _dec(raw: object) -> Decimal | None:
    """bean-query CSV gives amounts as strings, sometimes space-padded."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _month_key(iso: str) -> str | None:
    m = re.match(r"^(\d{4})-(\d{2})", iso.strip())
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _parse_date(iso: str) -> date | None:
    try:
        y, m, d = iso.strip().split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def _complete_months(first: date, last: date) -> list[str]:
    """Months WHOLLY inside the recorded range.

    The single most consequential function here. A book whose last entry is the
    4th of the month has a trailing month containing four days of spending; a
    run rate that includes it is understated by roughly the fraction of the
    month that has not happened yet, and nothing about the output would look
    wrong. The same applies to the first month, which usually starts partway in
    because that is when the customer began recording.

    So a month counts only if the recorded range covers it end to end.
    """
    out: list[str] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        # First day of this month, and of the next.
        start = date(year, month, 1)
        nyear, nmonth = (year + 1, 1) if month == 12 else (year, month + 1)
        next_start = date(nyear, nmonth, 1)
        if start >= first and next_start <= last_plus_one(last):
            out.append(f"{year:04d}-{month:02d}")
        year, month = nyear, nmonth
    return out


def last_plus_one(d: date) -> date:
    """The day after `d`, so a month ending on the 31st counts as covered."""
    try:
        return d.replace(day=d.day + 1)
    except ValueError:
        return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


class Assessment:
    """Runs the queries and folds them into facts. `book` needs only `.query`."""

    def __init__(self, book):
        self.book = book

    # ---- raw reads --------------------------------------------------------
    def _postings(self, pattern: str) -> list[dict]:
        return self.book.query(
            "SELECT date, account, payee, narration, number, currency "
            f"WHERE account ~ '{pattern}' ORDER BY date"
        )

    # ---- the assessment ---------------------------------------------------
    def run(self) -> dict:
        expenses = self._postings("Expenses")
        income = self._postings("Income")
        assets = self.book.query(
            "SELECT account, sum(number) AS amt, currency "
            "WHERE account ~ 'Assets' GROUP BY account, currency"
        )
        liabilities = self.book.query(
            "SELECT sum(number) AS amt, currency "
            "WHERE account ~ 'Liabilities' GROUP BY currency"
        )

        coverage = self._coverage(expenses + income)
        currencies = coverage["currencies"]
        multi = len(currencies) > 1

        monthly = self._monthly(expenses, income, coverage["complete_months"])
        cash = self._cash(assets)
        result = {
            "coverage": coverage,
            "monthly": monthly,
            "cash": cash,
            "liabilities": self._sum_by_currency(liabilities),
            "run_rate": self._run_rate(monthly, cash, coverage, multi),
            "categories": self._categories(expenses, coverage, multi),
            "movers": self._movers(expenses, coverage["complete_months"]),
            "outliers": self._outliers(expenses),
            "data_quality": self._data_quality(expenses, income, coverage),
        }
        return result

    def _coverage(self, rows: list[dict]) -> dict:
        dates = [d for d in (_parse_date(r.get("date", "")) for r in rows) if d]
        currencies = sorted(
            {(r.get("currency") or "").strip() for r in rows if (r.get("currency") or "").strip()}
        )
        if not dates:
            return {
                "first_date": None,
                "last_date": None,
                "days": 0,
                "complete_months": [],
                "posting_count": len(rows),
                "currencies": currencies,
                "sufficient": False,
                "why": "This book has no income or expense entries yet, so there is nothing to assess.",
            }
        first, last = min(dates), max(dates)
        months = _complete_months(first, last)
        return {
            "first_date": first.isoformat(),
            "last_date": last.isoformat(),
            "days": (last - first).days + 1,
            "complete_months": months,
            # Postings, NOT transactions — one transaction has at least two.
            # Naming it precisely because "14 transactions" off this number
            # would be a wrong sentence in the report.
            "posting_count": len(rows),
            "currencies": currencies,
            "sufficient": True,
            "why": "",
        }

    def _monthly(self, expenses, income, complete_months: list[str]) -> list[dict]:
        exp: dict[str, Decimal] = defaultdict(Decimal)
        inc: dict[str, Decimal] = defaultdict(Decimal)
        for row in expenses:
            key, amt = _month_key(row.get("date", "")), _dec(row.get("number"))
            if key and amt is not None:
                exp[key] += amt
        for row in income:
            key, amt = _month_key(row.get("date", "")), _dec(row.get("number"))
            if key and amt is not None:
                # Income postings are CREDITS and come back negative. Flipped
                # once, here, so nothing downstream has to remember.
                inc[key] += -amt
        out = []
        for key in sorted(set(exp) | set(inc)):
            e, i = exp.get(key, Decimal(0)), inc.get(key, Decimal(0))
            out.append({
                "month": key,
                "income": str(i),
                "expenses": str(e),
                "net": str(i - e),
                # A partial month is REPORTED — the customer wants to see this
                # month — but flagged, so nothing averages over it.
                "complete": key in complete_months,
            })
        return out

    def _cash(self, rows: list[dict]) -> dict:
        by_account, totals = [], defaultdict(Decimal)
        for row in rows:
            amt = _dec(row.get("amt"))
            cur = (row.get("currency") or "").strip()
            if amt is None:
                continue
            by_account.append({
                "account": row.get("account", ""),
                "amount": str(amt),
                "currency": cur,
            })
            totals[cur] += amt
        return {
            "by_account": by_account,
            "total_by_currency": {c: str(v) for c, v in totals.items()},
        }

    def _sum_by_currency(self, rows: list[dict]) -> dict:
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for row in rows:
            amt = _dec(row.get("amt"))
            if amt is not None:
                totals[(row.get("currency") or "").strip()] += amt
        return {c: str(v) for c, v in totals.items()}

    def _run_rate(self, monthly, cash, coverage, multi_currency: bool) -> dict:
        def no(why: str) -> dict:
            return {"sufficient": False, "why": why}

        if multi_currency:
            return no(
                "This book uses more than one currency "
                f"({', '.join(coverage['currencies'])}). Adding them together "
                "would be meaningless, and this does not hold exchange rates."
            )
        complete = [m for m in monthly if m["complete"]]
        if len(complete) < MIN_MONTHS_FOR_RUN_RATE:
            return no(
                f"Only {len(complete)} complete month(s) of data. A run rate "
                f"needs at least {MIN_MONTHS_FOR_RUN_RATE} to be a trend rather "
                "than a coincidence."
            )

        nets = sorted(Decimal(m["net"]) for m in complete)
        mid = len(nets) // 2
        median_net = nets[mid] if len(nets) % 2 else (nets[mid - 1] + nets[mid]) / 2
        avg_expenses = sum(Decimal(m["expenses"]) for m in complete) / len(complete)

        out = {
            "sufficient": True,
            "why": "",
            "months_used": [m["month"] for m in complete],
            "median_monthly_net": str(median_net),
            "average_monthly_expenses": str(avg_expenses),
            "runway_months": None,
            "runway_note": "",
        }

        currency = coverage["currencies"][0] if coverage["currencies"] else ""
        balance = _dec(cash["total_by_currency"].get(currency))
        if median_net >= 0:
            out["runway_note"] = (
                "The book is net positive across the complete months, so there "
                "is no burn to run out of."
            )
        elif balance is None or balance <= 0:
            out["runway_note"] = (
                "No positive cash balance is recorded, so a runway cannot be "
                "computed from this book."
            )
        else:
            out["runway_months"] = str((balance / -median_net).quantize(Decimal("0.1")))
            out["runway_note"] = (
                "Cash divided by the median monthly net across complete months. "
                "It assumes the next months look like the last ones, which is an "
                "assumption, not a finding."
            )
        return out

    def _categories(self, expenses, coverage, multi_currency: bool) -> list[dict]:
        if multi_currency:
            return []
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for row in expenses:
            amt = _dec(row.get("number"))
            if amt is not None:
                totals[row.get("account", "")] += amt
        grand = sum(totals.values())
        months = max(1, len([m for m in coverage["complete_months"]]))
        out = []
        for account, total in sorted(totals.items(), key=lambda kv: -kv[1]):
            out.append({
                "account": account,
                "total": str(total),
                "share_pct": str((total / grand * 100).quantize(Decimal("0.1")))
                if grand
                else "0",
                "monthly_average": str((total / months).quantize(Decimal("0.01"))),
            })
        return out

    def _movers(self, expenses, complete_months: list[str]) -> list[dict]:
        """Change between the last two COMPLETE months, per account.

        Complete months only. Comparing a full month against a half-recorded one
        manufactures a fall in spending in every category, every time.
        """
        if len(complete_months) < 2:
            return []
        prev_key, last_key = complete_months[-2], complete_months[-1]
        prev: dict[str, Decimal] = defaultdict(Decimal)
        last: dict[str, Decimal] = defaultdict(Decimal)
        for row in expenses:
            key, amt = _month_key(row.get("date", "")), _dec(row.get("number"))
            if amt is None:
                continue
            if key == prev_key:
                prev[row.get("account", "")] += amt
            elif key == last_key:
                last[row.get("account", "")] += amt

        out = []
        for account in sorted(set(prev) | set(last)):
            a, b = prev.get(account, Decimal(0)), last.get(account, Decimal(0))
            delta = b - a
            if delta == 0:
                continue
            out.append({
                "account": account,
                "from_month": prev_key,
                "to_month": last_key,
                "previous": str(a),
                "latest": str(b),
                "delta": str(delta),
                # A percentage against a zero baseline is not "infinite growth",
                # it is a new category. Say which.
                "pct_change": str((delta / a * 100).quantize(Decimal("0.1")))
                if a
                else None,
                "note": "new this month" if not a else "",
            })
        out.sort(key=lambda r: abs(Decimal(r["delta"])), reverse=True)
        return out

    def _outliers(self, expenses) -> list[dict]:
        by_account: dict[str, list[tuple[Decimal, dict]]] = defaultdict(list)
        for row in expenses:
            amt = _dec(row.get("number"))
            if amt is not None and amt > 0:
                by_account[row.get("account", "")].append((amt, row))

        out = []
        for account, entries in by_account.items():
            if len(entries) < MIN_SAMPLES_FOR_OUTLIER:
                continue
            amounts = sorted(a for a, _ in entries)
            mid = len(amounts) // 2
            median = (
                amounts[mid]
                if len(amounts) % 2
                else (amounts[mid - 1] + amounts[mid]) / 2
            )
            if median <= 0:
                continue
            for amt, row in entries:
                if amt >= median * OUTLIER_MULTIPLE:
                    out.append({
                        "date": row.get("date", ""),
                        "account": account,
                        "payee": row.get("payee", ""),
                        "narration": row.get("narration", ""),
                        "amount": str(amt),
                        "account_median": str(median),
                        "times_median": str((amt / median).quantize(Decimal("0.1"))),
                        # Stated so the report cannot call this a problem. It is
                        # a thing worth looking at, and often it is the annual
                        # invoice that is supposed to be there.
                        "note": "larger than usual for this account — not necessarily wrong",
                    })
        out.sort(key=lambda r: Decimal(r["times_median"]), reverse=True)
        return out

    def _data_quality(self, expenses, income, coverage) -> list[dict]:
        flags = []
        uncategorised = sum(
            1
            for r in expenses
            if re.search(r"(Uncategori[sz]ed|Misc|Other)$", r.get("account", ""))
        )
        if uncategorised:
            flags.append({
                "code": "uncategorised",
                "count": uncategorised,
                "message": f"{uncategorised} expense posting(s) sit in a catch-all account. Category figures will be misleading until they are moved.",
            })
        no_payee = sum(1 for r in expenses if not (r.get("payee") or "").strip())
        if no_payee:
            flags.append({
                "code": "missing_payee",
                "count": no_payee,
                "message": f"{no_payee} expense posting(s) have no payee, so they cannot be grouped by who was paid.",
            })
        if len(coverage["currencies"]) > 1:
            flags.append({
                "code": "multi_currency",
                "count": len(coverage["currencies"]),
                "message": "More than one currency is in use. Totals are reported per currency and never added together.",
            })
        if not income:
            flags.append({
                "code": "no_income",
                "count": 0,
                "message": "No income is recorded. Net and runway figures describe outgoings only.",
            })
        return flags


def assess(book) -> dict:
    return Assessment(book).run()
