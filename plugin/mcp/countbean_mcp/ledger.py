"""Git-backed Beancount ledger operations.

Every mutation follows the same safe loop: take the book's write lock, write to
a text file, run `bean-check`, and only commit if it validates. On failure the
working tree is restored, so a cloud book's ledger is never left in a broken
state.

Three properties make that sentence true rather than aspirational (#39), and all
three are load-bearing:

* **One writer at a time.** The HTTP sidecar's handlers are sync ``def``, so
  Starlette runs them on a 40-thread pool and a ``Ledger`` is built per request —
  no instance-level lock could serialise anything. Without the lock below, one
  request's commit stages another request's not-yet-validated text.
* **Commit is inside the try.** ``_commit`` can fail on a leftover
  ``.git/index.lock``, a missing git identity, or ENOSPC. When it did so outside
  the try, the caller was told 422 "the book is unchanged" while the text sat in
  the working tree, to be swept into the next writer's commit.
* **Only the touched files are staged.** ``git add -A`` committed whatever else
  happened to be in the tree under this request's message, which breaks the
  auditability the product sells: the message and its diff stop corresponding.

There is a gate in front of that loop as well (#105): ``add_directives`` accepts
dated ledger entries only. Validation is not a property of ``bean-check``, it is
a property of ``bean-check`` *running with the checks we think it has* — and a
single ``plugin`` or ``option`` line in the text a customer's LLM composed can
change which of those two sentences is true.
"""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

try:  # POSIX only; absent on Windows, where the thread lock still applies.
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None


class LedgerError(Exception):
    """Raised when an operation would leave the ledger invalid or fails.

    Maps to ``422 ledger_rejected``, whose documented meaning is *the book is
    unchanged*. Only raise it when that is true.
    """


class LedgerStateError(Exception):
    """The book may be left inconsistent and needs a human.

    Deliberately NOT a ``LedgerError``: this is the one case where we cannot
    promise the book is unchanged, so it must not be reported as a clean
    rejection. Maps to ``500 internal``.
    """


class LedgerTimeoutError(LedgerStateError):
    """A shelled-out command exceeded its budget and was killed (#172).

    A subclass so the existing ``LedgerStateError`` handler renders it
    ``500 internal`` — nothing new reaches the wire and CONTRACTS §4 is
    untouched.

    It is deliberately **not** a ``LedgerError``, and the reason is the
    customer's next action rather than the state of the tree. After a
    ``bean-check`` timeout the tree really is byte-identical — ``bean-check``
    does not write to the book and ``add_directives`` restores on the way out —
    so a ``422`` would not be lying about state. It would still be the wrong
    thing to say. ``422 ledger_rejected`` means *your directives were rejected*,
    which sends a customer off to edit input that was already correct. Our
    validator hanging is our failure, not their bad input, and ``500`` is what
    says so.

    Unlike its parent, this does not always mean the book needs a human: a
    killed ``bean-query`` leaves nothing behind at all. The message says which
    case this is; the class only decides the status code.
    """


# Directives are routed to a file by kind so the ledger stays readable.
ACCOUNTS_FILE = "accounts.beancount"
TRANSACTIONS_FILE = "transactions.beancount"
PRICES_FILE = "prices.beancount"

_OPEN_PREFIXES = ("open ", "close ", "commodity ", "note ", "document ")

# What a book will accept over the API (#105). This is an ALLOW-list on purpose:
# the text arriving here is composed by an LLM from a customer's prose, so the
# question is not "which directives are dangerous" — a denylist answers that one
# and is wrong the moment beancount grows a directive — but "which directives are
# the ledger CONTENT we sell".
#
# The property that separates them is not dated-vs-undated, close as that is.
# Beancount's configuration directives (`option`, `plugin`, `include`, `pushtag`,
# `pushmeta`, ...) are undated, and one of them — `option "insert_pythonpath"` /
# `plugin` — can switch validation off, which turns the whole validate-then-commit
# pipeline into a formality. But `custom` is DATED and is how Fava is configured
# (`custom "fava-option" ...`), which is the second half of what #105 describes.
# So: dated ledger entries by name, and nothing else.
_ALLOWED_DATED_KINDS = frozenset(
    {"open", "close", "commodity", "balance", "pad", "note", "document",
     "price", "event"}
)

# A transaction line is a date followed by `txn` or a single-character flag.
_TXN_FLAGS = frozenset("*!&#?%PSTCURM")

# Beancount accepts `-` and `/` as date separators. Matching both matters in the
# permissive direction only: a date form we fail to recognise is refused, which
# is the safe way to be wrong.
_DATE_RE = re.compile(r"\d{4}[-/]\d{2}[-/]\d{2}\Z")

# The write lock lives inside .git/ on purpose. Anything in the book directory
# proper would show up as untracked in `git status --porcelain` — the very
# invariant the lock exists to protect — and books provisioned before this
# change have a .gitignore we cannot retroactively edit.
LOCK_FILE = "countbean-write.lock"

# How often to retry the cross-process flock while waiting. Short enough that a
# freed lock is picked up promptly, long enough not to spin a CPU on a 256MB
# machine that is also running bean-check.
LOCK_POLL_SECONDS = 0.05

# Seconds a request will wait for the lock before giving up. bean-check on a
# large book is the slow step; a caller queued behind more than this is better
# told so than left hanging on a machine Fly may stop underneath it.
LOCK_TIMEOUT = 30.0

# Seconds a shelled-out command may RUN before it is killed (#172).
#
# LOCK_TIMEOUT above bounds how long a writer WAITS for the book. Nothing used
# to bound how long a writer may HOLD it: `subprocess.run` was called with no
# `timeout=`, so one bean-check that never returned wedged the book for writes
# until the process was killed, and every later write paid 30s and got a
# lock-timeout 422. On a scale-to-zero machine the request never completes, so
# the machine never stops either.
#
# Two budgets, because the two tools fail on different scales:
#
# * git works on a local repo of a few MB. The longest git operation measured in
#   this repo shape is a ~1.7s repack (the #119 harness, 1,305-commit fixture,
#   laptop /tmp). Past 30s git is not slow, it is stalled — a volume that
#   stopped answering, or a blocking `.git/index.lock`.
# * bean-check / bean-query parse the whole book, so they scale WITH it.
#   Measured uncached on beancount 3.2.3 (BEANCOUNT_DISABLE_LOAD_CACHE=1, this
#   laptop): 0.17s at 5k transactions, 0.54s at 20k, 1.40s at 50k / 5.55 MB —
#   ~28us per transaction, linear across that range. 120s is ~85x the 50k
#   figure.
#
# Both are STALL DETECTORS with orders of magnitude of headroom, not performance
# budgets. Neither should ever be reached by a book a customer could plausibly
# have. The measurements above are from a laptop; a shared-cpu-1x tenant machine
# is slower by a factor nobody has measured, which is why the headroom is this
# large rather than tight.
GIT_TIMEOUT = 30.0
BEAN_TIMEOUT = 120.0

# 🔴 bean-check must not trust Beancount's pickle load cache (#639, #195).
#
# A load that takes over 1.0s (PICKLE_CACHE_THRESHOLD, beancount/loader.py:79)
# leaves `.main.beancount.picklecache` next to main.beancount, and the next load
# returns it whenever `compute_input_hash(options_map["include"])` matches. That
# hash is over the ABSOLUTE include paths stored inside the cache, so a book
# directory that was copied, restored or pooled carries a cache that checks the
# ORIGINAL files' mtimes — and bean-check on the copy validates the original.
# Measured (storage-tenancy research, then re-run on a 75k-txn book): copy +
# an entry on an unknown account → bean-check rc=0. Through add_directives that
# is an invalid entry committed with the validator's blessing.
#
# So validation runs uncached. It costs a write nothing: every write changes a
# ledger file, which invalidates the cache anyway, and a disabled loader DELETES
# an existing cache (`delete_cache_function`), so the one bean-check that would
# have re-parsed still re-parses. Reads (bean-query) keep the cache on purpose —
# 6.9x faster on a big book, and a stale READ is recoverable where a stale
# VALIDATION commits. The same variable disables it at any value, "0" included
# (`os.getenv(...) is None`, loader.py:847), so it is set, never toggled.
_VALIDATION_ENV = {"BEANCOUNT_DISABLE_LOAD_CACHE": "1"}

# What a book's .gitignore holds. `*.picklecache` because bean-query (and Fava)
# still write the cache on a big book, and an untracked 14 MB pickle makes the
# #39 tree-clean invariant false on exactly the books nobody tests (#195). The
# glob matches the leading-dot `.main.beancount.picklecache`: gitignore globs,
# unlike the shell's, do not treat a leading dot specially.
GITIGNORE_LINES = ("*.xlsx", "*.html", "__pycache__/", "*.picklecache")

# One lock object per book root, shared by every Ledger instance in the process.
# Ledger is constructed per request, so the lock cannot live on the instance.
_ROOT_LOCKS: dict[Path, threading.Lock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(root: Path) -> threading.Lock:
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(root, threading.Lock())


# ---- currency precision (#639) ---------------------------------------------
#
# Currency precision: the book template's tolerances and the rounding rule (#639).
#
# Beancount has no idea how many decimals a currency has. It infers a tolerance
# per transaction from the numbers it is given, and an INTEGER number contributes
# nothing — so a transaction whose only VND amounts are whole numbers is checked
# against a tolerance of exactly zero. For a currency with no minor unit that is
# every transaction, and two things break:
#
# * **``@`` conversions into VND/JPY/KRW/IDR are refused.** ``-3396.63 USD @
#   25308.5 VND`` is 85,963,610.355 VND; the bank line says 85963610 VND, because
#   that is what a VND bank account holds, and bean-check reports "does not
#   balance (-0.355 VND)". Measured on main through the real
#   ``Ledger.add_directives``: **648 of 650** synthetic USD→VND invoices refused.
# * **``@@`` totals are refused too, for a different reason.** The parser turns
#   ``3.00 GBP @@ 100000 VND`` into a per-unit price of 33333.33…(28 digits), and
#   multiplying back leaves a 1e-23 residual against a zero tolerance. **124 of
#   650** refused. This one is not VND-specific: ``3 GBP @@ 4 USD`` with a
#   ``-4 USD`` leg fails in any book, for the same reason.
#
# The research put it at "68 of ~650"; the critic re-ran it and found the ``@@``
# division was only half the story, which is why this section has two halves.
#
# **The tolerance options** (``template_options``) go into every new book's
# ``main.beancount``, and ``Ledger.upgrade_template`` back-fills them. 0.5 of a
# unit for currencies with no minor unit is Beancount's own convention: its
# ``tolerance_multiplier`` is 0.5 (half the smallest digit), and
# ``quantize_with_tolerance`` quantizes an interpolated leg to ``2 × tolerance``,
# so ``VND:0.5`` is also what makes an amount-less VND leg come out in whole VND
# instead of 85963610.355 (and the rounding rule below books the 0.355). The
# ``*`` default fixes the ``@@`` artefact for every other currency.
#
# 🔴 The ``*`` default is ALSO the quantum for an amount-less leg in any currency
# the transaction gives no tolerance for — ``2 × *``, whatever the currency. The
# first version of this change set it to half a cent, and a crypto leg was
# silently rounded to 0.01: ``-500.00 USD @ 0.0000153 BTC`` with an amount-less
# wallet leg committed 0.01 BTC where main committed 0.00765 BTC (31% invented),
# with no error and no note. So ``*`` is NOT "every other currency behaves like a
# two-decimal one"; it is a tolerance small enough to round nothing anyone holds
# and still absorb the ``@@`` residual it exists for:
#
# * the residual is the parser's 28-digit division, so it scales with the
#   total: measured ≈ 3e-27 × total (4e-13 at a 1e14 total, 3e-11 at 1e16);
# * ``0.0000000005`` quantizes to 1e-9 — BTC's satoshi (1e-8), SOL's lamport
#   and ETH's gwei (1e-9) all survive — and still covers that residual with 10×
#   headroom at a 1e16 total. ETH/ERC-20 amounts below a gwei are the one
#   thing it rounds; they are not amounts an agent derives from a price.
#
# The three-decimal currencies are listed so a missing KWD leg rounds to fils,
# not to 1e-9 KWD: listing a currency is how it gets its real minor unit.
#
# **The rounding rule** (``settle_rounding``). A tolerance alone makes the
# transaction pass and the difference vanish: the critic measured 50,000 plain
# ``@`` invoices drifting the VND trial balance by +49.12, and
# ``option "account_rounding"`` does not capture it (still +49.12, rounding
# account at 0). What does is the thing an accountant expects: an explicit
# rounding-difference posting. So when a converted transaction misses whole-unit
# balance by no more than rounding can explain, the write path adds that posting
# itself, and says so in the commit message.
#
# What it will NOT absorb is a wrong rate. ``1145.07 GBP @ 32247.33 VND`` against
# the bank's 36,925,505 VND is 54.84 VND out — an LLM rounded the rate to 2 dp,
# and no rounding of the *amount* produces that. The bound below refuses it and
# bean-check says why; the accounting skill tells the agent to use the rate at
# full precision or an ``@@`` total instead.
#
# 🔴 This lives in ledger.py, not in a module of its own, because the plugin's
# vendored copy of this file is loaded standalone, by path
# (test_directive_allowlist.load_vendored_ledger): a relative import here is
# an ImportError on the customer's laptop, not a style choice.

# Currencies with no minor unit in practice. VND/JPY/KRW are ISO 4217 exponent 0;
# IDR is exponent 2 on paper but the rupiah has had no circulating sen for
# decades and Indonesian banks post whole rupiah. ISO has more exponent-0 codes
# (CLP, ISK, PYG, UGX, XAF, XOF, ...) — not listed because no market we serve
# uses them yet, and every entry here is an option line in every customer book.
ZERO_DECIMAL_CURRENCIES = ("VND", "JPY", "KRW", "IDR")

# ISO 4217 exponent-3 currencies. Listed so an amount-less leg comes out in fils,
# the way the bank posts it, rather than at the `*` default's 1e-9 (see the 🔴 in
# the section comment). (IQD is exponent 3 on paper and whole dinars in
# practice; left out.)
THREE_DECIMAL_CURRENCIES = ("BHD", "JOD", "KWD", "LYD", "OMR", "TND")

# Half the smallest unit, per Beancount's tolerance_multiplier convention.
_TOLERANCE_BY_EXPONENT = {0: "0.5", 3: "0.0005"}

# Every other currency: half a nano-unit. Beancount's own fallback is ZERO, which
# is what refuses `3 GBP @@ 4 USD` over a 1e-27 residual. 🔴 Not half a cent —
# see the 🔴 in the section comment: this is also the rounding quantum for every
# unlisted currency's amount-less leg, crypto included.
DEFAULT_TOLERANCE = "0.0000000005"

# Where explicit rounding differences are booked. An Expenses account because
# that is where a rounding difference sits in a chart of accounts; it takes both
# signs. Opened lazily — a book that never converts into a zero-decimal currency
# never sees it — and dated like Equity:Opening-Balances so it is open before
# any transaction a customer can have.
ROUNDING_ACCOUNT = "Expenses:Rounding"
ROUNDING_OPEN_DATE = "1970-01-01"

# How far a single `@` conversion can miss whole-unit balance through rounding
# the converted amount: half a unit. A transaction with n `@` legs into the
# currency can miss by n halves, if each leg's amount was rounded separately.
_ROUNDING_PER_CONVERSION = Decimal("0.5")

def _template() -> list[tuple[str, str, str]]:
    """(option, currency, value) for every template option line, in order."""
    rows = [("inferred_tolerance_default", "*", DEFAULT_TOLERANCE)]
    rows += [
        ("inferred_tolerance_default", c, _TOLERANCE_BY_EXPONENT[0])
        for c in ZERO_DECIMAL_CURRENCIES
    ]
    rows += [
        ("inferred_tolerance_default", c, _TOLERANCE_BY_EXPONENT[3])
        for c in THREE_DECIMAL_CURRENCIES
    ]
    # Render zero-decimal currencies in whole units. Without this, the display
    # context infers a precision from the numbers it has seen — and the
    # rounding postings above are fractional by design (0.355 VND), so a VND
    # book's balances would start rendering as 85963610.000 VND. reporting.py
    # parses those rendered strings, so this is also what the HTML/xlsx report
    # shows. `sum(number)` in BQL still returns the exact value.
    rows += [("display_precision", c, "1") for c in ZERO_DECIMAL_CURRENCIES]
    return rows


def template_options() -> list[str]:
    """The ``option`` lines every book's ``main.beancount`` carries (#639)."""
    return [f'option "{opt}" "{cur}:{value}"' for opt, cur, value in _template()]


def missing_template_options(main_text: str) -> list[str]:
    """The template option lines ``main_text`` lacks, in template order.

    Keyed by option and CURRENCY, not by exact line: a book whose owner already
    set ``VND:1`` keeps it. We add what is absent and never overwrite a choice.
    """
    missing = []
    for (opt, cur, _), line in zip(_template(), template_options()):
        pattern = rf'^\s*option\s+"{opt}"\s+"{re.escape(cur)}:'
        if not re.search(pattern, main_text, re.MULTILINE):
            missing.append(line)
    return missing


@dataclass(frozen=True)
class RoundingLine:
    """One rounding-difference posting the write path added."""

    date: str
    narration: str
    number: Decimal
    currency: str

    def describe(self) -> str:
        return (
            f"{self.date} \"{self.narration}\": posted {self.number:f} "
            f"{self.currency} to {ROUNDING_ACCOUNT}"
        )


def rounding_note(lines: list[RoundingLine]) -> str:
    """The commit-message paragraph that tells the caller what was added.

    It rides in ``CommitResult.message`` — the one field every transport already
    returns (CONTRACTS §4 rows 3/4, the plugin's "Committed x: message") — so the
    agent learns about the extra posting without a wire change. ``history``
    shows the subject line only, so the log stays one line per change.
    """
    currencies = sorted({line.currency for line in lines})
    head = (
        f"{', '.join(currencies)} {'has' if len(currencies) == 1 else 'have'} "
        "no minor unit, so the converted amount was rounded to a whole unit; "
        "the difference is booked explicitly rather than lost:"
    )
    return head + "\n" + "\n".join(f"- {line.describe()}" for line in lines)


def _code(line: str) -> str:
    """A line with any trailing ``;`` comment removed."""
    return line.split(";", 1)[0]


def settle_rounding(text: str) -> tuple[str, list[RoundingLine]]:
    """Add an explicit rounding posting where a conversion missed by rounding.

    Returns the (possibly) amended text and what was added. Anything this
    function does not fully understand is returned untouched, so bean-check
    stays the only judge of whether the text is valid — this can make a
    rounding difference explicit, never make an invalid entry acceptable:

    * text that does not parse → untouched (bean-check reports the error);
    * one amount-less posting → the same rule, computed the other way round.
      Beancount fills that leg in itself and, given the template's ``VND:0.5``,
      rounds it to a whole unit — so the fraction would vanish with no posting
      anywhere (main booked 85963610.355 VND into the bank; the first version
      of this change booked 85963610 and lost the 0.355 silently). The posting
      added here makes what the leg absorbs a whole number, so Beancount's
      rounding has nothing left to drop. Two or more amount-less postings, or
      one that would absorb more than one currency → skipped;
    * a posting at cost (``{...}``) → skipped. Its weight depends on the lots
      in the book, which this chunk cannot see;
    * a currency whose only conversions are ``@@`` totals → skipped. A total is
      exact by construction; the residual is the parser's division artefact,
      which the tolerance absorbs. Any larger miss is a wrong number, not
      rounding;
    * a residual larger than half a unit per ``@`` conversion → skipped, and
      bean-check refuses it. That is a wrong rate or amount, not rounding.
    """
    # Imported here, not at module top: the plugin imports this file on every
    # tool call, including the ones that never write, and beancount's parser is
    # the only thing in this section that costs anything to load.
    from beancount.core import interpolate
    from beancount.core.amount import Amount
    from beancount.core.data import Transaction
    from beancount.core.number import MISSING
    from beancount.parser import parser

    entries, errors, _ = parser.parse_string(text)
    if errors:
        return text, []

    def amountless(p) -> bool:
        return p.units is MISSING or (
            isinstance(p.units, Amount) and p.units.number is MISSING
        )

    lines = text.splitlines()
    insertions: list[tuple[int, str]] = []
    added: list[RoundingLine] = []

    for entry in entries:
        if not isinstance(entry, Transaction):
            continue
        postings = entry.postings
        # At most one leg Beancount fills in, and a plain one: no cost, no
        # price. Everything else must be written out in full.
        missing = [p for p in postings if amountless(p)]
        written = [p for p in postings if not amountless(p)]
        if len(missing) > 1 or any(
            p.cost is not None or p.price is not None for p in missing
        ):
            continue
        complete = all(
            isinstance(p.units, Amount)
            and p.units.number is not MISSING
            and isinstance(p.units.currency, str)
            and p.cost is None
            and (
                p.price is None
                or (p.price.number is not MISSING and isinstance(p.price.currency, str))
            )
            for p in written
        )
        if not complete or not written:
            continue

        # Classify every conversion into each currency by how it was WRITTEN.
        # The parser folds `@@` into a per-unit price and forgets it, so the
        # source line is the only place the distinction survives.
        per_unit: dict[str, int] = {}
        total: set[str] = set()
        for p in postings:
            if p.price is None:
                continue
            source = lines[p.meta["lineno"] - 1] if p.meta else ""
            if "@@" in _code(source):
                total.add(p.price.currency)
            else:
                per_unit[p.price.currency] = per_unit.get(p.price.currency, 0) + 1

        residual = list(interpolate.compute_residual(written))
        if missing:
            # The leg absorbs -residual, one posting per currency; only the
            # single-currency case is a rounding question. A leg that names
            # its currency must name that one, or bean-check has an error to
            # report and this has nothing to add.
            named = missing[0].units
            if len(residual) != 1 or (
                named is not MISSING and named.currency not in (MISSING, residual[0].units.currency)
            ):
                continue
            absorbed = residual[0].units.number
            # What Beancount's quantize would drop from that leg (half-even,
            # the decimal default it uses). Booking it makes the leg exact.
            dropped = absorbed - absorbed.quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
            residual = [(residual[0].units.currency, dropped)]
        else:
            residual = [(pos.units.currency, pos.units.number) for pos in residual]
        for cur, number in residual:
            if (
                cur not in ZERO_DECIMAL_CURRENCIES
                or number == 0
                or cur in total
                or cur not in per_unit
                or abs(number) > _ROUNDING_PER_CONVERSION * per_unit[cur]
            ):
                continue
            # normalize() so 14733.50000 - 14734 books as 0.5, not 0.50000;
            # formatted with "f" below so it can never render as 5E-1.
            correction = (-number).normalize()
            last = _last_line_of(lines, entry.meta["lineno"])
            indent = _indent_of(lines, postings)
            insertions.append((
                last,
                f"{indent}{ROUNDING_ACCOUNT}  {correction:f} {cur}"
                f"  ; rounding difference: {cur} has no minor unit",
            ))
            added.append(RoundingLine(
                date=entry.date.isoformat(),
                narration=entry.narration or entry.payee or "",
                number=correction,
                currency=cur,
            ))

    if not insertions:
        return text, []
    # Bottom-up, so an insertion never shifts a line number still to be used.
    for index, line in sorted(insertions, key=lambda item: item[0], reverse=True):
        lines.insert(index + 1, line)
    return "\n".join(lines), added


def _last_line_of(lines: list[str], header_lineno: int) -> int:
    """0-based index of a transaction's last line: its header plus every
    indented line that follows it (postings, their metadata, indented
    comments). A blank or column-0 line ends it — the same rule
    ``Ledger._route`` uses to tell one directive from the next."""
    index = header_lineno - 1
    while index + 1 < len(lines):
        following = lines[index + 1]
        if not following.strip() or not following[0].isspace():
            break
        index += 1
    return index


def _indent_of(lines: list[str], postings) -> str:
    """Indent the new posting the way the caller indented theirs."""
    first = lines[postings[0].meta["lineno"] - 1]
    return first[: len(first) - len(first.lstrip())] or "  "


def rounding_account_is_open(*texts: str) -> bool:
    """True if any of ``texts`` opens ``ROUNDING_ACCOUNT``."""
    pattern = (
        r"^\d{4}[-/]\d{2}[-/]\d{2}\s+open\s+"
        + re.escape(ROUNDING_ACCOUNT)
        + r"(\s|$)"
    )
    return any(re.search(pattern, t, re.MULTILINE) for t in texts)


def rounding_account_open_line() -> str:
    return f"{ROUNDING_OPEN_DATE} open {ROUNDING_ACCOUNT}"


def _insert_options(main_text: str, options: list[str]) -> str:
    """Insert ``options`` after the last top-of-file ``option`` line.

    Beancount reads options anywhere in the file, so placement is for the human
    reading main.beancount: with the other options, above the includes.
    """
    lines = main_text.splitlines()
    anchor = -1
    for index, line in enumerate(lines):
        if line.startswith("option "):
            anchor = index
        elif line.startswith("include "):
            break
    lines[anchor + 1:anchor + 1] = options
    return "\n".join(lines) + "\n"


def _moved_currencies(before, after) -> set[str]:
    """Currencies with a posting whose number differs between two reads.

    Compared as multisets of Decimals, so 0.00765 and 0.007650000 are the same
    amount — only a different VALUE counts as moved.
    """
    diff = (Counter(before) - Counter(after)) + (Counter(after) - Counter(before))
    return {currency for _, _, currency, _ in diff}


def _tolerances_that_move(missing: list[str], moved: set[str]) -> list[str]:
    """The template tolerance lines responsible for ``moved``.

    A listed currency answers to its own line; any other answers to ``*``.
    ``display_precision`` never changes a number, so it is never held.
    """
    listed = set(ZERO_DECIMAL_CURRENCIES) | set(THREE_DECIMAL_CURRENCIES)
    culprits = {c for c in moved if c in listed}
    if moved - listed:
        culprits.add("*")
    return [
        line for line in missing
        if line.startswith('option "inferred_tolerance_default"')
        and line.split('"')[3].split(":")[0] in culprits
    ]


def _held_note(held: list[str], moved: set[str]) -> str:
    return (
        f"adding {'; '.join(held)} would change amount-less postings already in "
        f"the book ({', '.join(sorted(moved))}). Book their rounding differences "
        "first, then upgrade again."
    )


@dataclass
class CommitResult:
    commit: str
    message: str


class Ledger:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).expanduser().resolve()
        self.main = self.root / "main.beancount"

    # ---- lifecycle -------------------------------------------------------
    def exists(self) -> bool:
        return self.main.exists()

    def init(self, name: str, currency: str = "USD") -> CommitResult:
        if self.exists():
            raise LedgerError(f"A book already exists at {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        # Tolerance options travel with every book, whatever its operating
        # currency: a USD book with one JPY card needs them as much as a VND
        # book does (#639; see the currency precision section above for the measurements).
        self.main.write_text(
            f'option "title" "{name}"\n'
            f'option "operating_currency" "{currency}"\n'
            + "".join(line + "\n" for line in template_options())
            + "\n"
            f'include "{ACCOUNTS_FILE}"\n'
            f'include "{TRANSACTIONS_FILE}"\n'
            f'include "{PRICES_FILE}"\n'
        )
        (self.root / ACCOUNTS_FILE).write_text(
            "; Account openings live here.\n"
            "1970-01-01 open Equity:Opening-Balances\n"
        )
        (self.root / TRANSACTIONS_FILE).write_text("; Transactions live here.\n")
        (self.root / PRICES_FILE).write_text("; Price directives live here.\n")
        (self.root / ".gitignore").write_text(
            "".join(line + "\n" for line in GITIGNORE_LINES)
        )

        if not (self.root / ".git").exists():
            self._git("init", "-q")
            self._git("config", "user.name", "Countbean")
            self._git("config", "user.email", "bot@countbean.app")
        self.validate()
        return self._commit(
            f"Initialise book: {name}",
            [
                ".gitignore",
                "main.beancount",
                ACCOUNTS_FILE,
                TRANSACTIONS_FILE,
                PRICES_FILE,
            ],
        )

    # ---- validation ------------------------------------------------------
    def validate(self) -> None:
        """Run bean-check, uncached; raise LedgerError with details on failure."""
        proc = self._run(
            "bean-check", str(self.main), check=False, env=_VALIDATION_ENV
        )
        if proc.returncode != 0:
            raise LedgerError(
                "bean-check failed:\n" + (proc.stderr or proc.stdout).strip()
            )

    # ---- concurrency + durable writes ------------------------------------
    @contextmanager
    def write_lock(self, timeout: float = LOCK_TIMEOUT):
        """Serialise mutations of this book, across threads and processes.

        Two layers, because they cover different failures:

        * a per-root :class:`threading.Lock` for the sidecar's own thread pool,
          which is where the measured corruption came from;
        * ``flock`` on a file under ``.git/`` for a second *process* — the MCP
          plugin pointed at the same directory, a cron, an operator in a shell.

        ``flock`` is released by the kernel when the holder dies, so unlike a
        lock *file* (the ``.git/index.lock`` failure this fixes) it cannot go
        stale and wedge the book.

        ``timeout`` bounds the whole wait, not just the first half. A blocking
        ``LOCK_EX`` would hold the request open indefinitely behind a live-but-
        wedged second process — rare, since flock dies with its holder, but a
        parameter named ``timeout`` that only covers one of two waits is the kind
        of half-true guarantee this module exists to stop making.
        """
        deadline = time.monotonic() + timeout
        thread_lock = _thread_lock_for(self.root)
        if not thread_lock.acquire(timeout=timeout):
            raise self._lock_timeout(timeout)
        handle = None
        try:
            git_dir = self.root / ".git"
            if fcntl is not None and git_dir.is_dir():
                handle = open(git_dir / LOCK_FILE, "w")
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            handle.close()
                            handle = None
                            raise self._lock_timeout(timeout) from None
                        time.sleep(LOCK_POLL_SECONDS)
            yield
        finally:
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
            thread_lock.release()

    @staticmethod
    def _lock_timeout(timeout: float) -> LedgerError:
        # A LedgerError, so it renders as 422 "the book is unchanged" — which is
        # exactly true: we never touched a file.
        return LedgerError(
            f"Timed out after {timeout:g}s waiting for another write to this "
            "book to finish."
        )

    def _tmp_path(self, path: Path) -> Path:
        """Scratch path for an atomic replace, on the same filesystem.

        Prefers ``.git/`` so a crash between write and replace cannot leave an
        untracked ``*.tmp`` dirtying the working tree forever.
        """
        git_dir = self.root / ".git"
        parent = git_dir if git_dir.is_dir() else self.root
        return parent / f".{path.name}.tmp"

    def _write_atomic(self, path: Path, content: str) -> None:
        """Replace ``path``'s contents in one step, or not at all.

        ``open("a")`` + ``write`` can be interrupted half way — Fly stops these
        machines routinely (``auto_stop_machines``) — leaving a truncated
        directive that bean-check will reject forever after.
        """
        tmp = self._tmp_path(path)
        try:
            with open(tmp, "w") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _restore(self, backups: dict[str, str]) -> None:
        """Put the snapshotted contents back, reporting a failed restore loudly.

        The old rollback was unguarded: if the second of two files failed to
        write, the resulting OSError *replaced* the LedgerError and the caller
        got a bare 500 with a half-restored book and no way to know. If we
        cannot restore, that is exactly what the caller must be told.
        """
        failed = []
        for name, original in backups.items():
            try:
                self._write_atomic(self.root / name, original)
            except OSError as exc:
                failed.append(f"{name}: {exc}")
        if failed:
            raise LedgerStateError(
                "The book could not be rolled back and may be inconsistent. "
                "Do not write to it again until it is repaired. Files: "
                + "; ".join(failed)
            )

    # ---- mutations -------------------------------------------------------
    def add_directives(self, text: str, message: str) -> CommitResult:
        """Append directives (auto-routed by kind), validate, then commit.

        If anything fails — validation, git, the OS — the working tree is
        restored and no commit is made. The commit is INSIDE the try because a
        commit failure is exactly the case where the caller was previously told
        "rejected, unchanged" over text that was still on disk.
        """
        text = text.strip()
        if not text:
            raise LedgerError("No directives provided.")

        # Before the lock and before anything touches disk: a refusal here is
        # the cheapest possible way to satisfy "422 means the book is unchanged".
        self._reject_configuration_directives(text)

        with self.write_lock():
            # Inside the lock: whether the rounding account needs opening is a
            # read of the book, and two writers that both saw it unopened would
            # both open it — the second then fails bean-check on a duplicate
            # `open` it never asked for.
            text, rounding = self._settle_rounding(text)
            if rounding:
                message = message + "\n\n" + rounding_note(rounding)
            routed = self._route(text)
            backups = {name: (self.root / name).read_text() for name in routed}
            try:
                for name, chunk in routed.items():
                    self._write_atomic(
                        self.root / name,
                        backups[name] + "\n" + chunk.strip() + "\n",
                    )
                self.validate()
                return self._commit(message, sorted(routed))
            except BaseException:
                # Every exception, not just LedgerError: an OSError or a
                # MemoryError between the append and the commit used to leave
                # the text on disk with no rollback and no commit.
                self._restore(backups)
                raise

    def _settle_rounding(self, text: str) -> tuple[str, list]:
        """Make a zero-decimal conversion's rounding difference explicit (#639).

        See the currency precision section: the posting is added only where rounding
        the converted amount to a whole unit explains the miss, and bean-check
        still judges the result. Runs after the #105 gate, so it only ever sees
        text that gate accepted, and adds nothing the gate would refuse — a
        posting and, the first time, an ``open``.
        """
        try:
            text, rounding = settle_rounding(text)
        except Exception:
            # Deliberately broad, and deliberately the only place that is. The
            # rule can only ever turn a refusal into an acceptance, so if it
            # trips over input nobody anticipated, the right answer is the one
            # every write got before #639: the caller's text, unamended, judged
            # by bean-check. Letting it raise would 500 a write that might well
            # have been valid.
            return text, []
        if rounding:
            accounts = self.root / ACCOUNTS_FILE
            existing = accounts.read_text() if accounts.exists() else ""
            if not rounding_account_is_open(existing, text):
                text = rounding_account_open_line() + "\n" + text
        return text, rounding

    def upgrade_template(self) -> CommitResult | None:
        """Bring an existing book up to the template ``init`` writes today.

        ``init`` is the only thing that writes ``main.beancount`` and
        ``.gitignore``, so a fix to either reaches new books only — the limit
        #195 recorded, and the reason ``LOCK_FILE`` lives in ``.git/``. This is
        the other half: add the tolerance options (#639) and the
        ``*.picklecache`` ignore (#195) where they are missing, validate, and
        commit, all under the write lock like any other write.

        Additive and idempotent. A tolerance the owner already set for a
        currency is kept; a second run finds nothing to do and returns None
        without committing. Option lines are what the #105 gate refuses over
        the API, and this does not go through that gate — the lines come from
        ``template_options()``, never from a caller, and a tolerance
        can only loosen what balances, never switch a check off.

        🔴 But a tolerance is also the quantum an amount-less leg is rounded
        to, so adding one can silently change amounts already in the book: a
        VND bank leg written without an amount on the old template holds
        85963610.355, and ``VND:0.5`` would re-book it as 85963610 with the
        0.355 nowhere. History is not ours to rewrite, so every posting is
        read before and after (exact numbers, uncached), and a tolerance line
        that moves one is held back — named in the commit message, or in the
        LedgerError when nothing else was left to commit. That book keeps the
        old behaviour for that currency until someone books the differences.

        ⚠️ Not called by anything automatically. ``start.sh`` runs it at boot
        only when ``BOOK_TEMPLATE_UPGRADE=1``: it writes a commit into a
        customer's history, which is a decision, not a side effect.
        """
        with self.write_lock():
            main_text = self.main.read_text()
            gitignore = self.root / ".gitignore"
            ignore_text = gitignore.read_text() if gitignore.exists() else ""

            changes: dict[str, str] = {}
            missing = missing_template_options(main_text)
            if missing:
                changes["main.beancount"] = _insert_options(main_text, missing)
            before = self._posting_numbers() if missing else None
            ignored = {line.strip() for line in ignore_text.splitlines()}
            absent = [line for line in GITIGNORE_LINES if line not in ignored]
            if absent:
                if ignore_text and not ignore_text.endswith("\n"):
                    ignore_text += "\n"
                changes[".gitignore"] = ignore_text + "".join(
                    line + "\n" for line in absent
                )
            if not changes:
                return None

            backups = {
                name: (self.root / name).read_text()
                if (self.root / name).exists() else None
                for name in changes
            }
            try:
                for name, content in changes.items():
                    self._write_atomic(self.root / name, content)
                self.validate()
                held: list[str] = []
                if before is not None:
                    moved = _moved_currencies(before, self._posting_numbers())
                    if moved:
                        held = _tolerances_that_move(missing, moved)
                        kept = [line for line in missing if line not in held]
                        if kept:
                            changes["main.beancount"] = _insert_options(main_text, kept)
                        else:
                            changes.pop("main.beancount")
                        self._write_atomic(self.main, changes.get("main.beancount", main_text))
                        self.validate()
                        still = _moved_currencies(before, self._posting_numbers())
                        if still:
                            raise LedgerError(
                                "Template upgrade would change amounts already in the "
                                f"book ({', '.join(sorted(still))}); nothing written."
                            )
                        if not changes:
                            raise LedgerError(
                                "Template upgrade held back: "
                                + _held_note(held, moved)
                            )
                message = "Upgrade book template: currency tolerances, ignore load cache"
                if held:
                    message += "\n\nHeld back: " + _held_note(held, moved)
                return self._commit(message, sorted(changes))
            except BaseException:
                for name, original in backups.items():
                    if original is None:
                        (self.root / name).unlink(missing_ok=True)
                self._restore({n: o for n, o in backups.items() if o is not None})
                raise

    def _posting_numbers(self) -> list[tuple[str, str, str, Decimal]]:
        """Every posting as the book books it: (date, account, currency, number).

        ``str(number)`` because the bare column comes back formatted for a
        terminal (measured: ``   -3896.63       ``, padded to the display
        context's width), and this compares values, not renderings. Uncached
        for the reason ``validate`` is: it judges the files on disk, not a
        pickle of somebody else's.
        """
        proc = self._run(
            "bean-query", "-f", "csv", str(self.main),
            "SELECT date, account, currency, str(number) AS n",
            check=False, env=_VALIDATION_ENV,
        )
        if proc.returncode != 0:
            raise LedgerError(
                "Query failed:\n" + (proc.stderr or proc.stdout).strip()
            )
        return [
            (row["date"], row["account"], row["currency"], Decimal(row["n"]))
            for row in csv.DictReader(io.StringIO(proc.stdout))
        ]

    def _reject_configuration_directives(self, text: str) -> None:
        """Refuse anything that is not a dated ledger entry (#105).

        ``_route`` sends everything it does not recognise to the transactions
        file, and every file it writes is *included* from ``main.beancount``.
        That is the whole reason this is worth its own pass: routing decides
        which file a line lands in, never whether it may land at all.

        Raises ``LedgerError`` — CONTRACTS §4 maps it to ``422 ledger_rejected``
        with this message, so the message names the offending kind and the line.
        """
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # Continuation lines (postings, metadata) are indented; a directive
            # begins at column 0. Same rule ``_route`` uses, deliberately.
            if not stripped or line[0].isspace() or stripped.startswith(";"):
                continue

            token = stripped.split()
            head = token[0]
            if _DATE_RE.match(head):
                kind = token[1] if len(token) > 1 else ""
                if kind == "txn" or (len(kind) == 1 and kind in _TXN_FLAGS):
                    continue
                if kind in _ALLOWED_DATED_KINDS:
                    continue
                named = kind or "(nothing after the date)"
            else:
                named = head

            raise LedgerError(
                f'Rejected: "{named}" is not a directive a book accepts '
                f"(line {number}). Writes may contain transactions and "
                + ", ".join(sorted(_ALLOWED_DATED_KINDS))
                + " entries only. Configuration directives — option, plugin, "
                "include, custom, pushtag, pushmeta and the like — can disable "
                "validation or re-point the book at other files, so they are "
                "not accepted over the API."
            )

    def _route(self, text: str) -> dict[str, str]:
        """Split a directive block into {filename: chunk} by directive kind."""
        buckets: dict[str, list[str]] = {}
        current = TRANSACTIONS_FILE
        for line in text.splitlines():
            stripped = line.strip()
            # A directive line begins at column 0 with a date or keyword.
            if stripped and not line[0].isspace():
                token = stripped.split(None, 1)
                head = token[0]
                rest = token[1] if len(token) > 1 else ""
                if any(rest.startswith(p) for p in _OPEN_PREFIXES) or head in (
                    "open", "close", "commodity", "note", "document",
                ):
                    current = ACCOUNTS_FILE
                elif head == "price":
                    current = PRICES_FILE
                else:
                    current = TRANSACTIONS_FILE
            buckets.setdefault(current, []).append(line)
        return {name: "\n".join(lines) for name, lines in buckets.items()}

    # ---- reads -----------------------------------------------------------
    def read_all(self) -> str:
        parts = []
        for name in (ACCOUNTS_FILE, TRANSACTIONS_FILE, PRICES_FILE):
            path = self.root / name
            if path.exists():
                parts.append(f"; ==== {name} ====\n" + path.read_text())
        return "\n".join(parts)

    def query(self, bql: str) -> list[dict[str, str]]:
        """Run a BQL query via bean-query and return rows as dicts."""
        proc = self._run("bean-query", "-f", "csv", str(self.main), bql, check=False)
        if proc.returncode != 0:
            raise LedgerError(
                "Query failed:\n" + (proc.stderr or proc.stdout).strip()
            )
        reader = csv.DictReader(io.StringIO(proc.stdout))
        return [dict(row) for row in reader]

    def accounts(self) -> list[str]:
        rows = self.query("SELECT DISTINCT account ORDER BY account")
        return [r["account"] for r in rows if r.get("account")]

    # ---- history ---------------------------------------------------------
    def history(self, n: int = 20) -> list[dict[str, str]]:
        proc = self._git(
            "log", f"-{n}", "--pretty=format:%h\x1f%ad\x1f%s", "--date=short",
        )
        out = []
        for line in proc.stdout.splitlines():
            h, date, subject = line.split("\x1f", 2)
            out.append({"commit": h, "date": date, "message": subject})
        return out

    def revert(self, ref: str) -> CommitResult:
        """Revert a commit, then prove the book still validates.

        A revert is a write like any other and had none of the safety: git
        applies it cleanly whenever the texts do not overlap, so reverting the
        commit that opened an account left a committed, bean-check-invalid book.
        Fava will not load it, every read endpoint 422s, and the only repair —
        another write — also fails validation. The book is wedged with no
        in-product way out.

        The tree-clean invariant does not catch this one: revert makes its own
        commit, so validating afterwards is a separate duty.
        """
        ref = ref.strip()
        if not ref or ref.startswith("-"):
            # `ref` is caller-supplied and goes into an argv list. There is no
            # shell here, so this is not RCE, but `--strategy-option=theirs`
            # would still be smuggled in as a git option.
            raise LedgerError(f"Not a valid commit reference: {ref!r}")

        with self.write_lock():
            resolved = self._git(
                "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False
            ).stdout.strip()
            if not resolved:
                raise LedgerError(f"Unknown commit: {ref}")

            before = self._git("rev-parse", "HEAD").stdout.strip()
            proc = self._git("revert", "--no-edit", resolved, check=False)
            if proc.returncode != 0:
                # A conflicting revert leaves conflict markers in the ledger and
                # .git/REVERT_HEAD set; abort so the next write does not commit
                # `<<<<<<<` into a customer's books.
                self._git("revert", "--abort", check=False)
                raise LedgerError(
                    f"Cannot revert {ref} cleanly:\n"
                    + (proc.stderr or proc.stdout).strip()
                )
            try:
                self.validate()
            except LedgerError:
                self._git("reset", "--hard", before, check=False)
                raise
            head = self._git("rev-parse", "--short", "HEAD").stdout.strip()
            return CommitResult(commit=head, message=f"Revert {ref}")

    # ---- plumbing --------------------------------------------------------
    def _commit(self, message: str, paths: list[str]) -> CommitResult:
        """Stage exactly ``paths`` and commit them.

        ``paths`` is required rather than defaulting to ``-A``: staging the whole
        tree committed anything else lying around — a Fava edit, a crashed
        request's leftovers — under this request's message, so the commit
        message and its diff stopped describing the same change.
        """
        self._git("add", "--", *paths)
        # Nothing staged? Return current head rather than erroring.
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            head = self._git("rev-parse", "--short", "HEAD", check=False).stdout.strip()
            return CommitResult(commit=head or "0000000", message="(no changes)")
        self._git("commit", "-q", "-m", message)
        head = self._git("rev-parse", "--short", "HEAD").stdout.strip()
        return CommitResult(commit=head, message=message)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run("git", *args, cwd=self.root, check=check)

    @staticmethod
    def _timeout_error(args: tuple[str, ...], budget: float) -> "LedgerTimeoutError":
        """Word the timeout by tool, because only one of them mutates.

        git is the only command here that writes to the book. Killing it can
        leave a commit that did complete, or a stale ``.git/index.lock`` that
        `flock` cannot clear because it is not a lock this code holds — so the
        caller has to be told to look before writing again. bean-check and
        bean-query only read; carrying the same alarm for them would train
        operators to ignore it.
        """
        aftermath = (
            " The repository may be left mid-operation: check `git status` in "
            "the book, and clear a stale .git/index.lock, before writing again."
            if args[0] == "git"
            else " The book was not modified."
        )
        return LedgerTimeoutError(
            f"`{args[0]}` did not finish within {budget:g}s and was killed "
            f"(command: {' '.join(args)})." + aftermath
        )

    def _run(
        self,
        *args: str,
        cwd: str | os.PathLike | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        if shutil.which(args[0]) is None:
            raise LedgerError(
                f"`{args[0]}` not found. Install the plugin's Python deps "
                f"(beancount, beanquery) — see the plugin README."
            )
        # Read the budget from the module at call time, not as a default
        # argument: tests lower it, and a default would bind it at import.
        budget = GIT_TIMEOUT if args[0] == "git" else BEAN_TIMEOUT
        try:
            proc = subprocess.run(
                list(args),
                cwd=cwd or self.root,
                capture_output=True,
                text=True,
                timeout=budget,
                # Overlaid on the inherited environment, never a replacement:
                # PATH is what finds bean-check in the first place.
                env={**os.environ, **env} if env else None,
            )
        except subprocess.TimeoutExpired as exc:
            # Never a LedgerError, whatever `check` says: a killed command is
            # not a rejection of the caller's input. See LedgerTimeoutError.
            raise self._timeout_error(args, budget) from exc
        if check and proc.returncode != 0:
            raise LedgerError(
                f"Command {' '.join(args)} failed:\n"
                + (proc.stderr or proc.stdout).strip()
            )
        return proc
