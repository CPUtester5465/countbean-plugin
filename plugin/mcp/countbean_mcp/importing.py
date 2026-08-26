"""Deterministic statement parsing: bytes in, PROPOSED transactions out (#515).

WHY THIS IS CODE AND NOT A PROMPT
---------------------------------
``plugin/commands/ingest.md`` used to carry the whole importer as instructions:
read the file, work out which column is the amount, decide whether a positive
number means money in or money out, then call ``add_transactions``. That works,
and it fails three ways that only code can fix.

It is **not reproducible**. Column mapping, sign convention and debit/credit
detection were re-derived on every run, so two imports of one statement could
disagree and nothing in the book recorded which reading had been used.

It is **not reachable**. The hosted chat agent runs ``python -m countbean_mcp``
as its only toolset (``hermes/render_config.py``) and has no command files, so
every ingestion path had to re-solve the problem from scratch.

And it **cannot be sold**, because the behaviour belonged to the model rather
than to the product.

WHAT THIS MODULE WILL AND WILL NOT DECIDE
-----------------------------------------
It decides *parsing*: which column is the date, what a number means, which way
the money moved, and whether this row is already in the book. It does not decide
*categorisation* — the counter-account it emits is a named placeholder, and
replacing it is the model's job, which is the part a model is genuinely good at.

**It never writes.** It returns a proposal and the beancount text that would
express it. ``add_directives`` remains the only write path, so the bean-check
gate and the git commit stay exactly where they are.

THE LINE BETWEEN "ASSUMED" AND "AMBIGUOUS"
-------------------------------------------
Two different remedies, and conflating them makes one of them useless.

An **assumption** is uniform across the file and checkable by looking at one
field: the delimiter, the date format, the sign convention, which column is the
amount. Those are reported in ``mapping`` — that is #515's own remedy, *"the
detected column mapping is returned in the response so the caller can see what
was assumed"* — and each is overridable by argument.

An **ambiguity** is per-row and unresolvable from the bytes: a date that is
valid read either way, a row with both a debit and a credit, an amount column
whose direction the file never states. Those are flagged ``!`` on the
transaction and carry a reason. Flagging the uniform assumptions too would put
a ``!`` on every row of every import and train callers to ignore it.

WHAT IS OUT OF SCOPE, DELIBERATELY
-----------------------------------
Not a bank feed. This takes bytes somebody already has. Nothing here connects to
a bank, and ``/roadmap``'s "no direct bank feeds" stays true.

Not receipts, images or OCR — that is #516 and a different problem.
"""
from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .ledger import LedgerError


class ImporterError(LedgerError):
    """The bytes could not be turned into proposals, and the book is untouched.

    A ``LedgerError`` subclass so it renders the way every other refusal in this
    package does — 422 ``ledger_rejected`` over HTTP, a raised MCP tool error on
    the plugin. That status' documented meaning is *the book is unchanged*, and
    for this module it is unconditionally true: nothing here opens the ledger for
    writing at all.
    """


# The key every proposed transaction carries, and the whole basis of duplicate
# detection. It lands in the book as beancount metadata when the caller writes
# the proposal, and the next import reads it back out of the ledger text.
#
# `import-id` rather than `id`: beancount metadata is a flat namespace shared
# with whatever else a customer or another tool writes, and a generic key is one
# collision away from a statement silently refusing to import.
IMPORT_ID_KEY = "import-id"

# Versioned so a future change to how the key is derived cannot silently make
# every previously-imported row look new. A `cb2:` prefix would simply not match
# a `cb1:` key, which double-books — so the prefix is a promise that the
# derivation below is frozen, and changing it is a migration, not an edit.
KEY_VERSION = "cb1"

# Placeholders for the leg this module refuses to guess. Sign picks between them,
# which is arithmetic and not categorisation: money that left the declared
# account went somewhere, money that arrived came from somewhere.
EXPENSE_PLACEHOLDER = "Expenses:Unclassified"
INCOME_PLACEHOLDER = "Income:Unclassified"

_DELIMITERS = (",", ";", "\t", "|")

# Ordered, and the order is the tie-break when a column's dates parse under more
# than one of them. ISO first because it is unambiguous; `%m/%d/%Y` ahead of
# `%d/%m/%Y` because US exports dominate the corpus this was built against.
# When two survive, the choice is recorded as ambiguous and every row is flagged
# — the order only decides which reading gets shown, never whether to say so.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d-%b-%Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%m/%d/%y",
    "%d/%m/%y",
    "%d.%m.%y",
    "%d-%b-%y",
)

# Guards `%Y%m%d` against an eight-digit reference number. A column of account
# numbers is not a column of dates, and without this the widest date format in
# the list is also the least discriminating.
_MIN_YEAR, _MAX_YEAR = 1900, 2100

_DATE_WORDS = ("date", "posted", "posting", "dato", "datum", "fecha", "buchung")
_DESC_WORDS = (
    "description", "desc", "payee", "merchant", "name", "memo", "details",
    "detail", "narrative", "particulars", "reference", "reason", "text",
)
_DEBIT_WORDS = (
    "debit", "withdrawal", "withdrawn", "money out", "paid out", "outflow",
    "charge", "spent", "soll", "out",
)
_CREDIT_WORDS = (
    "credit", "deposit", "money in", "paid in", "inflow", "received",
    "haben", "in",
)
_AMOUNT_WORDS = ("amount", "value", "montant", "betrag", "importe", "sum")
_BALANCE_WORDS = ("balance", "saldo", "solde", "running")
_CURRENCY_WORDS = ("currency", "ccy", "curr", "waehrung", "devise")

_CURRENCY_CODE_RE = re.compile(r"^[A-Z][A-Z0-9._'-]{1,22}$")
_AMOUNT_STRIP_RE = re.compile(r"[^\d,.\-+()]")

# Existing keys are read back out of the ledger TEXT rather than out of BQL.
# beanquery's metadata support differs by version and the plugin ships against a
# range of them; a regex over the text works identically on a local book and on
# whatever a hosted book's `/ledger` returns, which is the only property that
# matters here.
_EXISTING_KEY_RE = re.compile(
    r"^\s+" + re.escape(IMPORT_ID_KEY) + r":\s*\"([^\"]+)\"", re.M
)
_OPEN_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+open\s+(\S+)(?:\s+([A-Z][^;\n]*))?", re.M
)
_TXN_HEAD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+[*!]")
_POSTING_RE = re.compile(
    r"^\s+(?:[*!]\s+)?([A-Z][A-Za-z0-9-]*(?::[A-Z0-9][A-Za-z0-9-]*)+)\s+"
    r"(-?[\d,]+(?:\.\d+)?)\s"
)

_OFX_MARKERS = ("<OFX", "OFXHEADER", "<STMTTRN")
_STMTTRN_RE = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.S | re.I)
_OFX_TAG_RE = re.compile(r"<([A-Z0-9.]+)>([^<]*)", re.I)


# --------------------------------------------------------------- data shapes --
@dataclass
class Proposal:
    """One row of a statement, as a transaction nobody has written yet."""

    date: str
    flag: str
    narration: str
    amount: str
    currency: str
    account: str
    counter_account: str
    import_id: str
    source_row: int
    ambiguities: list[str] = field(default_factory=list)
    duplicate_of_existing: bool = False

    def to_beancount(self) -> str:
        lines = [f'{self.date} {self.flag} "{_quote(self.narration)}"']
        lines.append(f'  {IMPORT_ID_KEY}: "{self.import_id}"')
        lines.append(f"  {self.account}  {self.amount} {self.currency}")
        lines.append(f"  {self.counter_account}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "flag": self.flag,
            "narration": self.narration,
            "amount": self.amount,
            "currency": self.currency,
            "account": self.account,
            "counter_account": self.counter_account,
            "import_id": self.import_id,
            "source_row": self.source_row,
            "ambiguities": list(self.ambiguities),
            "beancount": self.to_beancount(),
        }


@dataclass
class _Row:
    """A parsed statement line, before it becomes a proposal."""

    source_row: int
    date: str
    description: str
    amount: Decimal
    currency: str
    external_id: str = ""
    ambiguities: list[str] = field(default_factory=list)
    # Which repeat of an identical (date, amount, description) triple this row
    # is. Part of the duplicate key; see `import_key`.
    ordinal: int = 0


# ------------------------------------------------------------------ decoding --
def decode_content(content, content_encoding: str = "auto") -> tuple[str, dict]:
    """Turn whatever the caller sent into text, and say what was assumed.

    The input is BYTES plus a declared account, never a path: the chat agent
    shares no filesystem with the plugin, so a path is a contract only one of
    the two callers can honour.

    Over MCP and over JSON, bytes travel base64-encoded, so ``"auto"`` has to
    tell a base64 blob from a CSV that was pasted straight in. The test is not a
    heuristic about what the decode looks like — it is that **the base64
    alphabet contains no delimiter**. Every CSV this module can parse contains a
    comma, semicolon, tab or pipe; every OFX contains ``<``. None of those are
    in ``A-Za-z0-9+/=``. A string made only of base64 characters that also
    decodes cleanly is therefore base64, and a statement never is.
    """
    if isinstance(content, (bytes, bytearray)):
        return _bytes_to_text(bytes(content)), {"content_encoding": "bytes"}

    if not isinstance(content, str):
        raise ImporterError(
            f"content must be text or bytes, got {type(content).__name__}."
        )
    if not content.strip():
        raise ImporterError("content is empty — there is nothing to import.")

    choice = (content_encoding or "auto").strip().lower()
    if choice not in ("auto", "text", "base64"):
        raise ImporterError(
            f"content_encoding must be 'auto', 'text' or 'base64', not "
            f"{content_encoding!r}."
        )

    if choice == "text":
        return content, {"content_encoding": "text"}
    if choice == "base64":
        return _bytes_to_text(_b64(content, strict=True)), {"content_encoding": "base64"}

    if _looks_like_base64(content):
        decoded = _b64(content, strict=False)
        if decoded is not None:
            return _bytes_to_text(decoded), {"content_encoding": "base64"}
    return content, {"content_encoding": "text"}


def _looks_like_base64(text: str) -> bool:
    packed = "".join(text.split())
    if len(packed) < 8 or len(packed) % 4:
        return False
    return re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", packed) is not None


def _b64(text: str, strict: bool):
    try:
        return base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        if strict:
            raise ImporterError(
                "content_encoding='base64' but the content is not valid base64."
            ) from None
        return None


def _bytes_to_text(raw: bytes) -> str:
    """Decode, preferring the encodings bank exports are actually written in.

    ``latin-1`` last and unconditional: it cannot fail, so a statement with one
    stray byte in a merchant name is imported with that name slightly wrong
    rather than refused entirely.
    """
    for codec in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def sniff_format(text: str, override: str = "") -> str:
    choice = (override or "auto").strip().lower()
    if choice in ("csv", "ofx"):
        return choice
    if choice in ("qfx",):
        return "ofx"
    if choice != "auto":
        raise ImporterError(
            f"file_format must be 'auto', 'csv', 'ofx' or 'qfx', not {override!r}."
        )
    head = text[:4096].upper()
    if any(marker in head for marker in _OFX_MARKERS):
        return "ofx"
    return "csv"


# ------------------------------------------------------------------- numbers --
def _parse_number(text: str, decimal_sep: str) -> Decimal | None:
    """Parse one money-shaped cell, or return None.

    Handles the four ways a statement writes a negative — a leading minus, a
    trailing minus, parentheses, and a ``DR``/``CR`` suffix — because all four
    are in the wild and three of them make ``Decimal()`` raise.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    negative = False
    upper = raw.upper()
    for suffix in (" DR", "DR", " CR", "CR"):
        if upper.endswith(suffix):
            if suffix.strip() == "DR":
                negative = True
            raw = raw[: len(raw) - len(suffix)].strip()
            break

    if raw.startswith("(") and raw.endswith(")"):
        negative = not negative
        raw = raw[1:-1].strip()

    raw = _AMOUNT_STRIP_RE.sub("", raw)
    if raw.endswith("-"):
        negative = not negative
        raw = raw[:-1]
    if raw.startswith("-"):
        negative = not negative
        raw = raw[1:]
    raw = raw.lstrip("+").strip()
    if not raw or not any(ch.isdigit() for ch in raw):
        return None

    thousands = "," if decimal_sep == "." else "."
    raw = raw.replace(thousands, "")
    raw = raw.replace(decimal_sep, ".")
    if raw.count(".") > 1 or not re.fullmatch(r"\d*\.?\d*", raw):
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return -value if negative else value


def detect_decimal_separator(values: list[str]) -> tuple[str, bool]:
    """Work out ``.`` vs ``,`` for a whole COLUMN, and say if it is settled.

    Per value it is often undecidable — ``1.234`` is one thousand two hundred
    and thirty four in Frankfurt and one and a bit in Chicago. Across a column
    it usually is not, because one value somewhere has cents. So the decision is
    made once, from all the values, and reported in the mapping.

    Returns ``(separator, determined)``. ``determined=False`` means every
    separator in the column was followed by exactly three digits and appeared
    alone, which is the one case that stays genuinely ambiguous — the rows are
    flagged rather than quietly read one way.
    """
    cleaned = [_AMOUNT_STRIP_RE.sub("", v or "") for v in values]
    cleaned = [c for c in cleaned if any(ch.isdigit() for ch in c)]
    if not cleaned:
        return ".", True

    saw_both = False
    for c in cleaned:
        if "." in c and "," in c:
            saw_both = True
            return ("." if c.rfind(".") > c.rfind(",") else ","), True
    for c in cleaned:
        if c.count(".") > 1:
            return ",", True
        if c.count(",") > 1:
            return ".", True
    for c in cleaned:
        for sep in (".", ","):
            if c.count(sep) == 1:
                tail = c.split(sep)[1]
                if len(tail) != 3:
                    return sep, True
    has_sep = any("." in c or "," in c for c in cleaned)
    if not has_sep or saw_both:
        return ".", True
    return ".", False


# --------------------------------------------------------------------- dates --
def _try_date(text: str, fmt: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, fmt)
    except ValueError:
        return None
    if not _MIN_YEAR <= parsed.year <= _MAX_YEAR:
        return None
    return parsed.strftime("%Y-%m-%d")


def detect_date_format(values: list[str]) -> tuple[list[str], bool]:
    """Every format that parses EVERY value in the column.

    A column, not a value, for the same reason as the decimal separator: one row
    with a day past the twelfth settles the whole file, and most files have one.
    When two formats survive, both readings are real and the caller is told —
    that is the ``!`` case #515 asks for, and the only honest one here.
    """
    present = [v for v in values if (v or "").strip()]
    if not present:
        return [], False
    survivors = [f for f in _DATE_FORMATS if all(_try_date(v, f) for v in present)]
    if not survivors:
        return [], False
    distinct = {tuple(_try_date(v, f) for v in present) for f in survivors}
    return survivors, len(distinct) == 1


# ----------------------------------------------------------------- CSV shape --
def _header_match(header: str, words: tuple[str, ...]) -> bool:
    """Match header WORDS, not substrings.

    Substring matching looked fine until `"in"` (a credit word, as in "Money
    In") matched `"Ending Balance"` and turned the balance column into the
    credit half of a debit/credit pair. Multi-word entries are still matched as
    phrases; single words must be a whole token, with a prefix allowance long
    enough that "debits" and "withdrawals" still match and "in" still does not.
    """
    low = _norm(header)
    tokens = low.split()
    for word in words:
        if " " in word:
            if word in low:
                return True
        elif word in tokens:
            return True
        elif len(word) >= 5 and any(t.startswith(word) for t in tokens):
            return True
    return False


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _detect_delimiter(text: str, override: str = "") -> tuple[str, str]:
    if override:
        if override == "\\t":
            override = "\t"
        return override, "argument"
    best, best_score = None, -1.0
    for delimiter in _DELIMITERS:
        try:
            rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)
                    if any((c or "").strip() for c in r)]
        except csv.Error:
            continue
        if not rows:
            continue
        counts = Counter(len(r) for r in rows)
        modal, hits = counts.most_common(1)[0]
        if modal < 2:
            continue
        score = modal * (hits / len(rows))
        if score > best_score:
            best, best_score = delimiter, score
    if best is None:
        raise ImporterError(
            "Could not find a column separator. Tried comma, semicolon, tab and "
            "pipe, and no two lines agreed on a column count under any of them. "
            "Pass delimiter= if the file uses something else."
        )
    return best, "detected"


def _looks_like_data(row: list[str]) -> bool:
    """A data row has both a date-shaped cell and a money-shaped cell.

    Both, because either alone matches the preamble junk banks put above the
    header — a statement period is a date, an opening balance is a number.
    """
    has_date = any(any(_try_date(c, f) for f in _DATE_FORMATS) for c in row)
    has_number = any(_parse_number(c, ".") is not None for c in row)
    return has_date and has_number


class _CsvTable:
    """Rows, headers, and the arithmetic that decides what each column is."""

    def __init__(self, text: str, delimiter: str):
        rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)
                if any((c or "").strip() for c in r)]
        if not rows:
            raise ImporterError("The file has no non-blank rows.")
        counts = Counter(len(r) for r in rows)
        self.width = counts.most_common(1)[0][0]

        first_data = next((i for i, r in enumerate(rows) if _looks_like_data(r)), None)
        if first_data is None:
            raise ImporterError(
                "No row in this file has both a date and an amount, so there is "
                "nothing here this importer recognises as a statement line. If "
                "it is a CSV with an unusual layout, pass columns= to say which "
                "column is which."
            )
        self.preamble = first_data
        if first_data > 0 and not _looks_like_data(rows[first_data - 1]):
            self.headers = [c.strip() for c in rows[first_data - 1]]
            self.header_row = first_data  # 1-based line number
        else:
            self.headers = []
            self.header_row = None
        self.headers += [""] * max(0, self.width - len(self.headers))

        self.rows = []
        self.row_numbers = []
        for index in range(first_data, len(rows)):
            row = rows[index]
            if len(row) < self.width:
                row = row + [""] * (self.width - len(row))
            self.rows.append(row[: self.width])
            self.row_numbers.append(index + 1)

    def column(self, index: int) -> list[str]:
        return [r[index] for r in self.rows]

    def label(self, index: int) -> str:
        return self.headers[index] or f"column {index}"


def _numeric_fraction(values: list[str]) -> float:
    present = [v for v in values if (v or "").strip()]
    if not present:
        return 0.0
    hits = sum(1 for v in present if _parse_number(v, ".") is not None)
    return hits / len(present)


def _date_fraction(values: list[str]) -> float:
    present = [v for v in values if (v or "").strip()]
    if not present:
        return 0.0
    hits = sum(
        1 for v in present if any(_try_date(v, f) for f in _DATE_FORMATS)
    )
    return hits / len(present)


def _resolve_column(spec, table: _CsvTable) -> int | None:
    """Turn a caller's ``columns=`` entry into an index, by header or number."""
    if spec is None or spec == "":
        return None
    if isinstance(spec, int):
        index = spec
    elif isinstance(spec, str) and spec.strip().lstrip("-").isdigit():
        index = int(spec.strip())
    else:
        wanted = _norm(str(spec))
        for i, header in enumerate(table.headers):
            if _norm(header) == wanted:
                return i
        raise ImporterError(
            f"columns= names {spec!r}, which is not a header in this file. "
            f"Headers are: {[h for h in table.headers if h]}"
        )
    if not 0 <= index < table.width:
        raise ImporterError(
            f"columns= names index {index}, but the file has {table.width} columns."
        )
    return index


# ------------------------------------------------------------------ CSV parse --
def _parse_csv(text: str, account: str, options: dict) -> tuple[list[_Row], dict, list[str], list[dict]]:
    delimiter, delimiter_source = _detect_delimiter(text, options.get("delimiter", ""))
    table = _CsvTable(text, delimiter)
    notes: list[str] = []
    overrides: list[str] = []
    columns = options.get("columns") or {}

    # ---- date column
    date_col = _resolve_column(columns.get("date"), table)
    if date_col is not None:
        overrides.append("date")
    else:
        scored = [
            (_date_fraction(table.column(i)),
             1 if _header_match(table.label(i), _DATE_WORDS) else 0,
             -i, i)
            for i in range(table.width)
        ]
        scored = [s for s in scored if s[0] >= 0.9]
        if not scored:
            raise ImporterError(
                "No column parses as dates in every row. Pass columns= with a "
                "date column, or date_format= if the dates use a form this "
                "importer does not know."
            )
        date_col = max(scored)[3]
    other_dates = [
        table.label(i) for i in range(table.width)
        if i != date_col and _date_fraction(table.column(i)) >= 0.9
    ]
    if other_dates:
        notes.append(
            f"More than one column parses as dates; used {table.label(date_col)!r}. "
            f"Also date-shaped: {other_dates}. Pass columns={{'date': ...}} to "
            f"choose a different one."
        )

    date_values = table.column(date_col)
    if options.get("date_format"):
        date_format = options["date_format"]
        overrides.append("date_format")
        date_determined = True
        bad = [v for v in date_values if (v or "").strip() and not _try_date(v, date_format)]
        if bad:
            raise ImporterError(
                f"date_format={date_format!r} does not parse {len(bad)} of "
                f"{len(date_values)} values, e.g. {bad[0]!r}."
            )
        survivors = [date_format]
    else:
        survivors, date_determined = detect_date_format(date_values)
        if not survivors:
            raise ImporterError(
                f"Could not read the dates in {table.label(date_col)!r} — no "
                f"single format parses every value. Pass date_format= (strptime "
                f"syntax, e.g. '%d/%m/%Y')."
            )
        date_format = survivors[0]

    # ---- numeric columns
    numeric = [
        i for i in range(table.width)
        if i != date_col and _numeric_fraction(table.column(i)) >= 0.9
        and any((v or "").strip() for v in table.column(i))
    ]

    balance_col = _resolve_column(columns.get("balance"), table)
    debit_col = _resolve_column(columns.get("debit"), table)
    credit_col = _resolve_column(columns.get("credit"), table)
    amount_col = _resolve_column(columns.get("amount"), table)
    for name, value in (("balance", balance_col), ("debit", debit_col),
                        ("credit", credit_col), ("amount", amount_col)):
        if value is not None:
            overrides.append(name)

    if balance_col is None:
        balance_col = next(
            (i for i in numeric if _header_match(table.label(i), _BALANCE_WORDS)), None
        )
    if debit_col is None and credit_col is None:
        debit_col = next(
            (i for i in numeric
             if i != balance_col and _header_match(table.label(i), _DEBIT_WORDS)), None
        )
        credit_col = next(
            (i for i in numeric
             if i not in (balance_col, debit_col)
             and _header_match(table.label(i), _CREDIT_WORDS)), None
        )
        if (debit_col is None) != (credit_col is None):
            # One half of a pair is not a pair. A lone "Debit" column is a
            # signed amount column with an unhelpful name.
            amount_col = amount_col if amount_col is not None else (
                debit_col if debit_col is not None else credit_col
            )
            debit_col = credit_col = None
    if debit_col is None and credit_col is None and amount_col is None:
        pair = _exclusive_pair(table, [i for i in numeric if i != balance_col])
        if pair:
            debit_col, credit_col = pair
            notes.append(
                f"No debit/credit headers, but {table.label(debit_col)!r} and "
                f"{table.label(credit_col)!r} are never both filled on one row, "
                f"so they were read as a debit/credit pair (left column = money "
                f"out). Pass columns= to swap them."
            )

    shape_override = (options.get("amount_shape") or "auto").strip().lower()
    if shape_override not in ("auto", "signed", "debit_credit", "balance"):
        raise ImporterError(
            f"amount_shape must be 'auto', 'signed', 'debit_credit' or "
            f"'balance', not {options.get('amount_shape')!r}."
        )

    if amount_col is None and debit_col is None:
        candidates = [i for i in numeric if i != balance_col]
        if candidates:
            named = [i for i in candidates if _header_match(table.label(i), _AMOUNT_WORDS)]
            amount_col = named[0] if named else max(candidates, key=lambda i: (
                _moneyness(table.column(i)), -i
            ))

    if shape_override != "auto":
        shape = shape_override
        overrides.append("amount_shape")
    elif debit_col is not None and credit_col is not None:
        shape = "debit_credit"
    elif amount_col is not None:
        shape = "signed"
    elif balance_col is not None:
        shape = "balance"
    else:
        raise ImporterError(
            "No amount could be found. Columns seen: "
            f"{[table.label(i) for i in range(table.width)]}. Pass columns= "
            "with 'amount', or 'debit' and 'credit', or 'balance'."
        )
    if shape == "balance" and balance_col is None:
        raise ImporterError(
            "amount_shape='balance' needs a balance column; none was found. "
            "Pass columns={'balance': ...}."
        )
    if shape == "debit_credit" and (debit_col is None or credit_col is None):
        raise ImporterError(
            "amount_shape='debit_credit' needs both a debit and a credit "
            "column. Pass columns={'debit': ..., 'credit': ...}."
        )
    if shape == "signed" and amount_col is None:
        raise ImporterError(
            "amount_shape='signed' needs an amount column; none was found. "
            "Pass columns={'amount': ...}."
        )

    # ---- description column
    desc_col = _resolve_column(columns.get("description"), table)
    if desc_col is not None:
        overrides.append("description")
    else:
        used = {date_col, balance_col, debit_col, credit_col, amount_col}
        text_cols = [i for i in range(table.width) if i not in used]
        if not text_cols:
            desc_col = None
        else:
            named = [i for i in text_cols if _header_match(table.label(i), _DESC_WORDS)]
            pool = named or text_cols
            desc_col = max(pool, key=lambda i: (
                sum(len((v or "").strip()) for v in table.column(i)) / max(1, len(table.rows)),
                -i,
            ))
    if desc_col is None:
        notes.append("No description column found; narrations will be empty.")

    # ---- currency column
    currency_col = _resolve_column(columns.get("currency"), table)
    if currency_col is not None:
        overrides.append("currency")
    else:
        currency_col = next(
            (i for i in range(table.width)
             if _header_match(table.label(i), _CURRENCY_WORDS)
             and all(_CURRENCY_CODE_RE.match((v or "").strip().upper())
                     for v in table.column(i) if (v or "").strip())),
            None,
        )

    # ---- decimal separator, from every money-shaped column at once
    money_cols = [c for c in (amount_col, debit_col, credit_col, balance_col) if c is not None]
    money_values = [v for c in money_cols for v in table.column(c)]
    if options.get("decimal_separator"):
        decimal_sep = options["decimal_separator"]
        if decimal_sep not in (".", ","):
            raise ImporterError("decimal_separator must be '.' or ','.")
        decimal_determined = True
        overrides.append("decimal_separator")
    else:
        decimal_sep, decimal_determined = detect_decimal_separator(money_values)

    # ---- rows, in file order first
    parsed: list[dict] = []
    skipped: list[dict] = []
    for offset, row in enumerate(table.rows):
        line = table.row_numbers[offset]
        iso = _try_date(row[date_col], date_format)
        if iso is None:
            skipped.append({"row": line, "reason": "no date in the date column",
                            "text": delimiter.join(row)[:160]})
            continue
        entry = {
            "row": line,
            "date": iso,
            "description": (row[desc_col].strip() if desc_col is not None else ""),
            "debit": _parse_number(row[debit_col], decimal_sep) if debit_col is not None else None,
            "credit": _parse_number(row[credit_col], decimal_sep) if credit_col is not None else None,
            "amount": _parse_number(row[amount_col], decimal_sep) if amount_col is not None else None,
            "balance": _parse_number(row[balance_col], decimal_sep) if balance_col is not None else None,
            "currency": (row[currency_col].strip().upper() if currency_col is not None else ""),
        }
        parsed.append(entry)

    if not parsed:
        raise ImporterError("No row survived date parsing — nothing to propose.")

    order = _row_order([e["date"] for e in parsed])
    chronological = list(reversed(parsed)) if order == "reverse-chronological" else parsed

    sign_choice = (options.get("sign") or "auto").strip().lower()
    if sign_choice not in ("auto", "normal", "inverted"):
        raise ImporterError("sign must be 'auto', 'normal' or 'inverted'.")

    rows, sign, sign_source, shape_notes = _apply_shape(
        shape, chronological, sign_choice, options.get("opening_balance", ""),
        skipped, decimal_sep,
    )
    notes.extend(shape_notes)
    if sign_choice != "auto":
        overrides.append("sign")

    if not date_determined:
        reading = ", ".join(survivors)
        for row in rows:
            row.ambiguities.append(
                f"the date order cannot be settled from this file — "
                f"{reading} all parse every value; read here as {date_format}. "
                f"Pass date_format= to fix it."
            )
    if not decimal_determined:
        for row in rows:
            row.ambiguities.append(
                "every separator in the amount columns is followed by exactly "
                "three digits, so '.'/',' cannot be told apart as decimal or "
                "thousands; read here as a decimal point. Pass "
                "decimal_separator= to fix it."
            )

    mapping = {
        "file_format": "csv",
        "delimiter": delimiter,
        "delimiter_source": delimiter_source,
        "header_row": table.header_row,
        "preamble_rows_skipped": table.preamble - (1 if table.header_row else 0),
        "columns": {
            "date": _label_or_none(table, date_col),
            "description": _label_or_none(table, desc_col),
            "amount": _label_or_none(table, amount_col),
            "debit": _label_or_none(table, debit_col),
            "credit": _label_or_none(table, credit_col),
            "balance": _label_or_none(table, balance_col),
            "currency": _label_or_none(table, currency_col),
        },
        "amount_shape": shape,
        "date_format": date_format,
        "date_format_determined": date_determined,
        "date_formats_that_also_fit": survivors[1:] if not date_determined else [],
        "decimal_separator": decimal_sep,
        "decimal_separator_determined": decimal_determined,
        "sign": sign,
        "sign_source": sign_source,
        "row_order_in_file": order,
        "overrides_applied": sorted(set(overrides)),
    }
    return rows, mapping, notes, skipped


def _label_or_none(table: _CsvTable, index):
    return None if index is None else table.label(index)


def _moneyness(values: list[str]) -> float:
    """How much a numeric column looks like money rather than a reference number.

    A check number and a transaction amount are both numeric, and a file with
    both has two candidates for one role. Cents and minus signs separate them,
    and nothing else in the cell does.
    """
    present = [(v or "").strip() for v in values if (v or "").strip()]
    if not present:
        return 0.0
    fractional = sum(1 for v in present if re.search(r"[.,]\d{1,2}\b", v))
    negative = sum(1 for v in present if "-" in v or "(" in v)
    return (fractional + negative) / len(present)


def _exclusive_pair(table: _CsvTable, candidates: list[int]) -> tuple[int, int] | None:
    """Two numeric columns that are never both filled on the same row."""
    for left in candidates:
        for right in candidates:
            if right <= left:
                continue
            both = 0
            left_used = right_used = 0
            for row in table.rows:
                l_filled = bool((row[left] or "").strip())
                r_filled = bool((row[right] or "").strip())
                both += l_filled and r_filled
                left_used += l_filled
                right_used += r_filled
            if both == 0 and left_used and right_used:
                return left, right
    return None


def _row_order(dates: list[str]) -> str:
    first = dates[0]
    for value in dates[1:]:
        if value != first:
            return "chronological" if value > first else "reverse-chronological"
    return "chronological"


def _apply_shape(shape, entries, sign_choice, opening_balance, skipped, decimal_sep):
    """Turn parsed cells into a signed delta on the DECLARED account.

    Everything downstream — the key, the posting, the placeholder — depends only
    on that one number, so the three amount shapes converge here and nowhere
    else. #515 asks for them handled explicitly rather than inferred once, which
    is why this is a branch on a decided shape and not a cascade of guesses.
    """
    notes: list[str] = []
    rows: list[_Row] = []

    if shape == "debit_credit":
        sign = "normal" if sign_choice in ("auto", "normal") else "inverted"
        sign_source = (
            "argument" if sign_choice != "auto"
            else "bank convention: the debit column is money out of the account"
        )
        flip = Decimal(-1) if sign == "normal" else Decimal(1)
        for entry in entries:
            debit, credit = entry["debit"], entry["credit"]
            ambiguities = []
            if debit and credit:
                amount = flip * debit + (-flip) * credit
                ambiguities.append(
                    f"both the debit ({debit}) and the credit ({credit}) column "
                    f"are filled on this row; netted to {amount}"
                )
            elif debit:
                amount = flip * debit
            elif credit:
                amount = (-flip) * credit
            else:
                skipped.append({"row": entry["row"],
                                "reason": "neither the debit nor the credit column has a value"})
                continue
            rows.append(_make_row(entry, amount, ambiguities))
        return rows, sign, sign_source, notes

    if shape == "signed":
        amounts = [e["amount"] for e in entries]
        balances = [e["balance"] for e in entries]
        sign, sign_source, extra = _decide_sign(sign_choice, amounts, balances)
        notes.extend(extra)
        flip = Decimal(1) if sign == "normal" else Decimal(-1)
        for entry in entries:
            if entry["amount"] is None:
                skipped.append({"row": entry["row"],
                                "reason": "the amount column is empty"})
                continue
            rows.append(_make_row(entry, flip * entry["amount"], []))
        if sign_source.startswith("undetermined"):
            for row in rows:
                row.ambiguities.append(
                    "every amount in this file has the same sign and there is no "
                    "running balance to check it against, so the file never says "
                    "which direction the money moved; read here as "
                    f"'{sign}'. Pass sign='inverted' if that is backwards."
                )
        return rows, sign, sign_source, notes

    # shape == "balance": the amount is the change in the running balance.
    sign = "normal" if sign_choice in ("auto", "normal") else "inverted"
    sign_source = "derived from the change in the running balance"
    flip = Decimal(1) if sign == "normal" else Decimal(-1)
    previous = None
    if opening_balance:
        previous = _parse_number(str(opening_balance), decimal_sep)
        if previous is None:
            raise ImporterError(
                f"opening_balance={opening_balance!r} is not a number."
            )
    for entry in entries:
        current = entry["balance"]
        if current is None:
            skipped.append({"row": entry["row"], "reason": "the balance column is empty"})
            continue
        if previous is None:
            skipped.append({
                "row": entry["row"],
                "reason": "first row of a running-balance file: there is no "
                          "earlier balance to subtract, so its amount cannot be "
                          "derived. Pass opening_balance= to recover it.",
            })
            previous = current
            continue
        rows.append(_make_row(entry, flip * (current - previous), []))
        previous = current
    if not rows:
        raise ImporterError(
            "A running-balance file needs at least two rows to produce one "
            "amount, and this one produced none."
        )
    return rows, sign, sign_source, notes


def _decide_sign(choice, amounts, balances):
    """Say which way a signed amount column points, and on what evidence.

    Three outcomes, in descending order of how much they are worth:

    * **verified** — the file carries a running balance, so the deltas either
      match the amounts or match their negatives, and arithmetic settles it.
    * **mixed signs** — somebody wrote minus signs into the file on purpose, so
      the file is describing direction itself.
    * **undetermined** — every amount points the same way and nothing checks it.
      Read as ``normal`` and every row flagged, because a column of positive
      numbers is exactly as consistent with a month of spending as with a month
      of deposits, and no amount of staring at it decides which.
    """
    notes: list[str] = []
    if choice != "auto":
        return choice, "argument", notes

    pairs = [(a, b) for a, b in zip(amounts, balances) if a is not None and b is not None]
    if len(pairs) >= 2:
        agree = disagree = 0
        for index in range(1, len(pairs)):
            delta = pairs[index][1] - pairs[index - 1][1]
            amount = pairs[index][0]
            if abs(delta - amount) <= Decimal("0.005"):
                agree += 1
            elif abs(delta + amount) <= Decimal("0.005"):
                disagree += 1
        checked = agree + disagree
        if checked and agree >= max(1, int(checked * 0.8)):
            return "normal", "verified against the running balance", notes
        if checked and disagree >= max(1, int(checked * 0.8)):
            return "inverted", "verified against the running balance", notes
        if checked:
            notes.append(
                f"There is a running balance, but its row-to-row change matches "
                f"the amount column on only {agree} of {checked} rows and its "
                f"negative on {disagree}. The balance column was not used to "
                f"settle the sign; 'normal' was assumed."
            )

    signs = {1 if a > 0 else -1 for a in amounts if a is not None and a != 0}
    if len(signs) > 1:
        return "normal", "the amount column contains both signs, so the file states direction itself", notes
    return "normal", "undetermined — assumed, and every row is flagged", notes


def _make_row(entry: dict, amount: Decimal, ambiguities: list[str]) -> _Row:
    return _Row(
        source_row=entry["row"],
        date=entry["date"],
        description=entry["description"],
        amount=amount,
        currency=entry.get("currency", ""),
        ambiguities=list(ambiguities),
    )


# ------------------------------------------------------------------ OFX parse --
def _parse_ofx(text: str, account: str, options: dict) -> tuple[list[_Row], dict, list[str], list[dict]]:
    """Read ``<STMTTRN>`` blocks out of OFX 1.x SGML or OFX 2.x / QFX XML.

    One scanner for both dialects: OFX 1.x leaves tags unclosed
    (``<TRNAMT>-4.50``) and 2.x closes them (``<TRNAMT>-4.50</TRNAMT>``), and
    "value runs to the next ``<``" is correct for both.

    An OFX file is the easy case and worth saying why: ``TRNAMT`` is signed by
    the specification, so there is no sign convention to detect, and ``FITID``
    is a bank-assigned per-transaction identifier, so duplicate detection is
    exact rather than derived.
    """
    blocks = _STMTTRN_RE.findall(text)
    if not blocks:
        # 1.x files have no closing </STMTTRN> in some exports; fall back to
        # splitting on the opening tag rather than refusing the file.
        pieces = re.split(r"<STMTTRN>", text, flags=re.I)[1:]
        blocks = [p.split("</BANKTRANLIST>")[0].split("<STMTTRN")[0] for p in pieces]
    if not blocks:
        raise ImporterError(
            "This looks like OFX but contains no <STMTTRN> blocks, so there are "
            "no transactions in it."
        )

    default_currency = ""
    match = re.search(r"<CURDEF>\s*([A-Z]{3})", text, re.I)
    if match:
        default_currency = match.group(1).upper()

    sign_choice = (options.get("sign") or "auto").strip().lower()
    if sign_choice not in ("auto", "normal", "inverted"):
        raise ImporterError("sign must be 'auto', 'normal' or 'inverted'.")
    sign = "normal" if sign_choice in ("auto", "normal") else "inverted"
    flip = Decimal(1) if sign == "normal" else Decimal(-1)

    rows: list[_Row] = []
    skipped: list[dict] = []
    for index, block in enumerate(blocks, start=1):
        fields = {}
        for tag, value in _OFX_TAG_RE.findall(block):
            tag = tag.upper()
            if tag not in fields:
                fields[tag] = value.strip()
        posted = fields.get("DTPOSTED", "")
        digits = re.sub(r"\D", "", posted.split("[")[0])[:8]
        iso = _try_date(digits, "%Y%m%d")
        amount = _parse_number(fields.get("TRNAMT", ""), ".")
        if iso is None or amount is None:
            skipped.append({
                "row": index,
                "reason": f"missing or unreadable DTPOSTED/TRNAMT "
                          f"({posted!r}/{fields.get('TRNAMT', '')!r})",
            })
            continue
        name = fields.get("NAME", "").strip()
        memo = fields.get("MEMO", "").strip()
        parts = [p for p in (name, memo) if p]
        if len(parts) == 2 and parts[1].lower() in parts[0].lower():
            parts = parts[:1]
        rows.append(_Row(
            source_row=index,
            date=iso,
            description=" / ".join(parts),
            amount=flip * amount,
            currency=(fields.get("CURSYM", "") or default_currency).upper(),
            external_id=fields.get("FITID", "").strip(),
        ))

    if not rows:
        raise ImporterError("No <STMTTRN> block had a usable date and amount.")

    mapping = {
        "file_format": "ofx",
        "fields": {
            "date": "DTPOSTED",
            "amount": "TRNAMT",
            "description": "NAME + MEMO",
            "identifier": "FITID",
            "currency": "CURDEF",
        },
        "amount_shape": "signed",
        "sign": sign,
        "sign_source": ("argument" if sign_choice != "auto"
                        else "the OFX specification: TRNAMT is signed, negative is money out"),
        "overrides_applied": ["sign"] if sign_choice != "auto" else [],
        "statements_seen": len(re.findall(r"<CURDEF>", text, re.I)) or 1,
    }
    notes = []
    missing_ids = sum(1 for r in rows if not r.external_id)
    if missing_ids:
        notes.append(
            f"{missing_ids} of {len(rows)} transactions have no FITID, so their "
            f"duplicate key falls back to a content hash of date, amount and "
            f"description."
        )
    return rows, mapping, notes, skipped


# ----------------------------------------------------------------- book facts --
def existing_import_ids(book_text: str) -> set[str]:
    return set(_EXISTING_KEY_RE.findall(book_text or ""))


def account_currencies(book_text: str) -> dict[str, list[str]]:
    """Which currencies each ``open`` directive pinned an account to.

    Read for two reasons. It supplies the operating currency for a CSV that does
    not state one — the account the caller declared already says what it holds.
    And an account opened as ``open Expenses:Foo USD`` is PINNED: a posting in
    any other currency fails bean-check with "Invalid currency", at the write,
    long after the proposal looked fine.
    """
    out: dict[str, list[str]] = {}
    for account, currencies in _OPEN_RE.findall(book_text or ""):
        codes = [c.strip() for c in (currencies or "").replace(",", " ").split()]
        out[account] = [c for c in codes if _CURRENCY_CODE_RE.match(c)]
    return out


def existing_postings(book_text: str) -> Counter:
    """A multiset of ``(date, account, amount)`` already in the book.

    The weaker of the two duplicate checks, and it exists for one case the
    strong one cannot cover: entries booked before this tool existed carry no
    ``import-id``, so re-importing the statement they came from would look
    entirely new. A hit here NEVER suppresses a proposal — it flags it ``!`` —
    because "same day, same account, same amount" is also what two identical
    coffees look like.
    """
    counts: Counter = Counter()
    current_date = ""
    for line in (book_text or "").splitlines():
        head = _TXN_HEAD_RE.match(line)
        if head:
            current_date = head.group(1)
            continue
        if not current_date:
            continue
        posting = _POSTING_RE.match(line)
        if posting:
            account, amount = posting.group(1), posting.group(2).replace(",", "")
            try:
                counts[(current_date, account, str(Decimal(amount)))] += 1
            except InvalidOperation:
                continue
    return counts


# ---------------------------------------------------------------------- keys --
def import_key(account: str, row: _Row) -> str:
    """The stable per-row identity a re-import must recognise.

    An OFX ``FITID`` is the bank's own identifier for the transaction, so it is
    used whenever it exists. A CSV has nothing like it, so the key is a hash of
    the fields a bank does not rewrite between exports — the date, the signed
    amount, the currency and the description with its whitespace and case
    normalised away — scoped to the account being imported.

    The ordinal is what makes it a per-ROW key rather than a per-VALUE one. Two
    identical coffees on one day are two transactions and must both be booked;
    hashing without an ordinal collapses them into one, which is a different
    wrong answer from double-booking but just as wrong.
    """
    if row.external_id:
        material = f"ofx\x1f{account}\x1f{row.external_id}"
    else:
        description = " ".join((row.description or "").split()).lower()
        material = "\x1f".join([
            "csv", account, row.date, _fmt(row.amount), row.currency,
            description, str(row.ordinal),
        ])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{KEY_VERSION}:{digest}"


def _assign_ordinals(rows: list[_Row]) -> None:
    seen: Counter = Counter()
    for row in rows:
        signature = (row.date, _fmt(row.amount), " ".join((row.description or "").split()).lower())
        row.ordinal = seen[signature]
        seen[signature] += 1


# ------------------------------------------------------------------ rendering --
def _fmt(amount: Decimal) -> str:
    exponent = -amount.as_tuple().exponent
    places = max(2, exponent if isinstance(exponent, int) else 2)
    quantized = amount.quantize(Decimal(1).scaleb(-places))
    text = f"{quantized:f}"
    return "0.00" if text in ("-0.00", "-0") else text


def _quote(text: str) -> str:
    """Make a narration safe to sit inside a beancount string literal.

    Double quotes become single ones and control characters go: a merchant name
    with a stray ``"`` in it would otherwise close the string early and produce a
    parse error at the write, which is the worst place to find out.
    """
    cleaned = "".join(ch if ch.isprintable() else " " for ch in (text or ""))
    return " ".join(cleaned.replace('"', "'").split())


# ------------------------------------------------------------------- the tool --
def propose(
    content,
    account: str,
    *,
    book_text: str = "",
    currency: str = "",
    file_format: str = "auto",
    content_encoding: str = "auto",
    amount_shape: str = "auto",
    columns=None,
    date_format: str = "",
    delimiter: str = "",
    decimal_separator: str = "",
    sign: str = "auto",
    opening_balance: str = "",
    counter_account: str = "",
) -> dict:
    """Parse a statement into proposed transactions. Writes nothing, ever.

    ``book_text`` is the book's current plain text, used only to read back
    ``import-id`` metadata, to find what currency the declared account is pinned
    to, and to spot entries that look like the ones being proposed. Passing it
    empty is legal and means duplicate detection has nothing to compare against
    — the caller is told so in the response rather than left to assume it ran.
    """
    account = (account or "").strip()
    if not account:
        raise ImporterError(
            "An account is required — say which of the book's accounts this "
            "statement belongs to, e.g. account='Assets:Checking'. The rows in "
            "a bank export are all one side of that account and nothing in the "
            "file says which one it is."
        )
    if not re.match(r"^(Assets|Liabilities|Equity|Income|Expenses):", account):
        raise ImporterError(
            f"{account!r} is not a beancount account name. It must start with "
            f"Assets:, Liabilities:, Equity:, Income: or Expenses:."
        )

    columns = _coerce_columns(columns)
    text, decode_meta = decode_content(content, content_encoding)
    fmt = sniff_format(text, file_format)
    options = {
        "amount_shape": amount_shape,
        "columns": columns,
        "date_format": date_format,
        "delimiter": delimiter,
        "decimal_separator": decimal_separator,
        "sign": sign,
        "opening_balance": opening_balance,
    }

    if fmt == "ofx":
        rows, mapping, notes, skipped = _parse_ofx(text, account, options)
    else:
        rows, mapping, notes, skipped = _parse_csv(text, account, options)
    mapping.update(decode_meta)

    # ---- currency
    opens = account_currencies(book_text)
    pinned = opens.get(account, [])
    if currency:
        resolved_currency, currency_source = currency.strip().upper(), "argument"
    elif all(r.currency for r in rows):
        resolved_currency, currency_source = "", "the file's own currency column"
    elif len(pinned) == 1:
        resolved_currency, currency_source = pinned[0], f"the book's `open {account} {pinned[0]}`"
    else:
        raise ImporterError(
            f"Cannot tell what currency these amounts are in. The file does not "
            f"say, and {account} "
            + (f"is opened for {pinned}, which is more than one currency"
               if pinned else "is not open in the book yet")
            + ". Pass currency='USD' (or whichever), or open the account first."
        )
    for row in rows:
        if not row.currency:
            row.currency = resolved_currency
    if pinned:
        wrong = sorted({r.currency for r in rows if r.currency not in pinned})
        if wrong:
            notes.append(
                f"{account} is opened as `open {account} {' '.join(pinned)}`, "
                f"which PINS it: postings in {wrong} will be refused by "
                f"bean-check with 'Invalid currency' when this is written. "
                f"There is no tool to amend an existing open directive."
            )

    # ---- keys, then duplicates
    rows.sort(key=lambda r: (r.date, r.source_row))
    _assign_ordinals(rows)
    known = existing_import_ids(book_text)
    soft = existing_postings(book_text) if book_text else Counter()

    proposals: list[Proposal] = []
    duplicates: list[dict] = []
    for row in rows:
        key = import_key(account, row)
        if row.amount == 0:
            skipped.append({
                "row": row.source_row,
                "reason": "the amount is zero, so there is nothing to book",
            })
            continue
        placeholder = counter_account.strip() or (
            INCOME_PLACEHOLDER if row.amount > 0 else EXPENSE_PLACEHOLDER
        )
        if key in known:
            duplicates.append({
                "import_id": key, "date": row.date, "amount": _fmt(row.amount),
                "narration": _quote(row.description), "source_row": row.source_row,
                "reason": "this exact row is already in the book, keyed by its "
                          "import-id metadata",
            })
            continue
        ambiguities = list(row.ambiguities)
        signature = (row.date, account, str(Decimal(_fmt(row.amount))))
        if soft[signature] > 0:
            soft[signature] -= 1
            ambiguities.append(
                "the book already holds an entry on this date, on this account, "
                "for this amount, and it carries no import-id — so it may be "
                "this same transaction booked by hand or by an earlier import. "
                "Proposed anyway, flagged rather than dropped."
            )
        proposals.append(Proposal(
            date=row.date,
            flag="!" if ambiguities else "*",
            narration=_quote(row.description),
            amount=_fmt(row.amount),
            currency=row.currency,
            account=account,
            counter_account=placeholder,
            import_id=key,
            source_row=row.source_row,
            ambiguities=ambiguities,
        ))

    block = "\n\n".join(p.to_beancount() for p in proposals)
    needed = sorted({p.counter_account for p in proposals} | {account})
    missing = [a for a in needed if a not in opens]
    earliest = min((p.date for p in proposals), default="1970-01-01")

    return {
        "wrote_anything": False,
        "account": account,
        "source": {
            "bytes": len(text.encode("utf-8")),
            "format": fmt,
            "rows_parsed": len(rows),
            "rows_skipped": skipped,
        },
        "mapping": mapping,
        "currency": {"value": resolved_currency or "per row", "source": currency_source},
        "proposed": [p.to_dict() for p in proposals],
        "counts": {
            "proposed": len(proposals),
            "flagged_for_review": sum(1 for p in proposals if p.flag == "!"),
            "skipped_as_already_imported": len(duplicates),
        },
        "duplicates": {
            "strategy": (
                "each row carries an import-id in its metadata: the bank's FITID "
                "for OFX, otherwise a hash of account, date, signed amount, "
                "currency and normalised description plus an ordinal for "
                "identical repeats. Re-importing the same statement finds those "
                "ids already in the ledger and proposes nothing."
            ),
            "book_text_supplied": bool(book_text),
            "existing_ids_in_book": len(known),
            "already_imported": duplicates,
        },
        "accounts_to_open": missing,
        "open_directives": "\n".join(f"{earliest} open {a}" for a in missing),
        "beancount": block,
        "notes": notes + ([
            f"The counter-account on every proposal is a PLACEHOLDER "
            f"({INCOME_PLACEHOLDER} / {EXPENSE_PLACEHOLDER} by sign). This tool "
            f"parses; it does not categorise. Replace them before writing."
        ] if proposals and not counter_account else []),
    }


def _coerce_columns(columns) -> dict:
    if columns in (None, "", {}):
        return {}
    if isinstance(columns, dict):
        return columns
    if isinstance(columns, str):
        try:
            parsed = json.loads(columns)
        except json.JSONDecodeError as exc:
            raise ImporterError(
                f"columns= must be a JSON object like "
                f'{{"date": "Posted Date", "amount": "Amount"}} — {exc}'
            ) from None
        if not isinstance(parsed, dict):
            raise ImporterError("columns= must be a JSON object, not a list or scalar.")
        return parsed
    raise ImporterError(f"columns= must be a JSON object, got {type(columns).__name__}.")
