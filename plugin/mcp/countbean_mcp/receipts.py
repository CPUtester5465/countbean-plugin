"""Receipt photo / PDF -> a PROPOSED transaction (#516).

WHAT THIS FILE IS, AND WHAT IT DELIBERATELY IS NOT
--------------------------------------------------
It is the part of "photo -> transaction" that must be the same every time: what
counts as legible, what happens to a field that is not, what the resulting
Beancount text looks like, and which of the ways a write can fail are knowable
BEFORE the write. All of it is pure — dicts in, dicts out, no I/O — so a test
can pin it and two invocations cannot disagree.

It is **not** an OCR engine and there is no OCR vendor anywhere in the product.
#516 settled that: on the chat path the customer's photo arrives as an inline
image part on the metered endpoint (CONTRACTS §3.10) and the reading is the
agent's own vision turn, measured at $0.00087 per receipt; on the plugin path
the customer's own Claude reads it and we are billed nothing at all. So the
caller hands this module WHAT IT READ, with a confidence per field, and this
module decides what may be believed.

⚠️ THE CONFIRMATION STEP IS PART OF THE CONTRACT, NOT THE CALLER'S MANNERS.
Nothing here writes and nothing here can be made to write. Everything below the
floor comes back flagged rather than as a value, and the rendered directive
carries Beancount's `!` — so a caller that ignores every confidence in the
response still produces a PENDING entry a human has to look at, rather than a
confident wrong one. That property is asserted in
`platform/apps/book-runtime/tests/test_plugin_receipts.py`.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal, InvalidOperation
from pathlib import Path

#: Largest receipt this will hand to the control-plane. Mirrors
#: `RECEIPT_MAX_BYTES` there (10 MB) so an oversize file is refused on the
#: caller's machine, before it is base64-expanded by a third and pushed over
#: somebody's phone tether to be refused at the far end.
MAX_RECEIPT_BYTES = 10 * 1024 * 1024

# ---------------------------------------------------------------------------
# The floor.
#
# 0.6 is a judgement, not a measurement, and it is written here once so it can
# be moved with evidence rather than argued at each call site. What IS measured
# is the failure it exists to stop: a receipt total read wrong is not a smaller
# version of a receipt total read right, it is a wrong number in a ledger that
# balances, and nothing downstream will ever catch it.
#
# 🔴 A CALLER MAY RAISE IT AND MAY NOT LOWER IT. `effective_floor` takes the
# max. A floor a caller can set to zero is not a floor, and the caller here is a
# language model composing an argument list.
# ---------------------------------------------------------------------------
CONFIDENCE_FLOOR = 0.6


def effective_floor(requested: float | None) -> float:
    try:
        asked = float(requested) if requested is not None else CONFIDENCE_FLOOR
    except (TypeError, ValueError):
        asked = CONFIDENCE_FLOOR
    return max(CONFIDENCE_FLOOR, min(asked, 1.0))


# ---------------------------------------------------------------------------
# What kind of file this is.
#
# BY MAGIC BYTES, NEVER BY FILENAME. The filename is caller input on both paths
# and on the chat path it is whatever a phone called the attachment. It never
# reaches the bucket key either (see the control-plane's `receiptKey`), so the
# only thing that decides how a receipt is stored and served is its content.
# ---------------------------------------------------------------------------
_PREFIX_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# ISO base-media brands a phone writes for a HEIF still. `ftyp` sits at offset 4.
_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"mif1", b"msf1", b"heif"}

#: What the control-plane will accept (`RECEIPT_CONTENT_TYPES`). Anything this
#: function can return that is NOT in here is refused with its real name, which
#: is a far more useful error than "unsupported".
STORABLE = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}
)


def sniff_content_type(data: bytes) -> str:
    """The media type of `data`, or `""` when we do not recognise it."""
    for prefix, media in _PREFIX_MAGIC:
        if data.startswith(prefix):
            return media
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "image/heic"
    return ""


class ReceiptInputError(ValueError):
    """The caller gave us something that is not a file we can store."""


def read_input(file_path: str = "", content_base64: str = "") -> bytes:
    """Bytes from exactly one of a path or a base64 blob.

    Two sources because the two callers genuinely differ: Claude Code has the
    customer's filesystem and a chat agent has no filesystem in common with
    anything. EXACTLY one, because accepting both and silently preferring one is
    how a caller ends up storing the wrong file with no error to read.
    """
    path, blob = (file_path or "").strip(), (content_base64 or "").strip()
    if bool(path) == bool(blob):
        raise ReceiptInputError(
            "Give exactly one of `file_path` (a receipt on this machine) or "
            "`content_base64` (raw bytes). Both or neither is ambiguous, and a "
            "receipt stored from the wrong source is evidence for the wrong "
            "entry."
        )
    if path:
        target = Path(path).expanduser()
        try:
            data = target.read_bytes()
        except OSError as e:
            raise ReceiptInputError(f"Could not read {target}: {e}") from None
    else:
        # Strict: base64 that silently skips unrecognised characters decodes a
        # damaged upload into a shorter, valid-looking image whose hash nobody
        # can reproduce from the original.
        try:
            data = base64.b64decode(blob, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ReceiptInputError(f"content_base64 is not valid base64: {e}") from None
    if not data:
        raise ReceiptInputError("That file is empty; there is nothing to store.")
    if len(data) > MAX_RECEIPT_BYTES:
        raise ReceiptInputError(
            f"That file is {len(data)} bytes and the limit is "
            f"{MAX_RECEIPT_BYTES}. Photograph the receipt rather than scanning "
            f"it at print resolution — a legible photo is well under this."
        )
    return data


def store(book, data: bytes, content_type: str, hosted: bool) -> dict:
    """Put the bytes somewhere durable and return the reference the ledger keeps.

    HOSTED: the control-plane's §3.12 route, which derives the bucket key from
    the book this api-key already authenticates for. Nothing about the key is
    ours to choose and nothing about it is the customer's.

    LOCAL: `~/.countbean/receipts/<sha256>`, beside the reports the plugin
    already writes. ⚠️ DELIBERATELY NOT INSIDE THE BOOK. A book is a git repo
    and #119 measured a 1 GB volume filling in 3-5 years on text alone, because
    every commit stores a complete new blob of a linearly growing file; binaries
    committed into that repo turn years into weeks. The reference the ledger
    carries says `local` so a proposal cannot be mistaken for one whose evidence
    we hold.
    """
    sha = hashlib.sha256(data).hexdigest()
    if hosted:
        payload = book.store_receipt(
            base64.b64encode(data).decode("ascii"), content_type, sha
        )
        stored = payload.get("receipt") or {}
        stored.setdefault("storage", "hosted")
        return stored

    out_dir = Path.home() / ".countbean" / "receipts"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / sha
    target.write_bytes(data)
    return {
        "key": str(target),
        "sha256": sha,
        "bytes": len(data),
        "content_type": content_type,
        "storage": "local",
    }


def next_step_for(content_type: str) -> str:
    """What the agent has to do next, which differs for a PDF and it matters.

    ⚠️ MEASURED, NOT ASSUMED: `api_server.py:264-268` in hermes-agent 0.14.0 —
    the adapter a hosted book runs — raises
    `unsupported_content_type: … uploaded files and document inputs are not
    supported on this endpoint` for every `file` / `input_file` part, while
    accepting inline `image_url` data URLs at :231-262. So a PDF can be STORED
    through this tool but cannot be shown to the model on the chat path, and
    saying that here is the difference between one clear sentence and a customer
    watching an agent invent a receipt it never saw.
    """
    if content_type == "application/pdf":
        return (
            "The PDF is stored. Now READ IT YOURSELF — this tool does no OCR. "
            "If you are a chat agent, you cannot: the chat-completions adapter "
            "accepts inline images and refuses document parts, so ask for a "
            "PHOTO of the receipt instead of a PDF. Do not propose a "
            "transaction from a document you have not read."
        )
    return (
        "The image is stored. Now READ IT YOURSELF — this tool does no OCR, by "
        "design. Then call `propose_receipt_transaction` with what you read and "
        "an honest confidence for each field."
    )


# ---------------------------------------------------------------------------
# The currency-pinning trap, paid for once already.
#
# `2026-01-01 open Expenses:Foo USD` PINS that account to USD. A posting in any
# other currency fails bean-check with "Invalid currency", and there is no tool
# in the product that amends an existing `open` — `add_directives` can only
# append, and a second `open` for the same account is itself an error.
#
# So a receipt in a foreign currency landing on a pinned account fails AT THE
# WRITE, after the customer has approved a proposal that looked fine. Reading
# the constraints here moves that failure to the proposal, where it is one
# sentence naming the account instead of a bean-check dump.
# ---------------------------------------------------------------------------
_OPEN_RE = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2}\s+open\s+([A-Z][A-Za-z0-9:_-]*)\s*(.*?)\s*$"
)
_CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9._-]{0,22}[A-Z0-9]$")


def open_currency_constraints(ledger_text: str) -> dict[str, list[str]]:
    """account -> the currencies its `open` pins it to (`[]` = unconstrained).

    An account ABSENT from the returned mapping has never been opened, which is
    the other way a write fails after an approved proposal — and it is worth
    exactly the same early sentence.
    """
    out: dict[str, list[str]] = {}
    for line in ledger_text.splitlines():
        if ";" in line:
            line = line.split(";", 1)[0]
        m = _OPEN_RE.match(line)
        if not m:
            continue
        account, rest = m.group(1), m.group(2)
        # A booking method is a quoted string after the currencies; stop there.
        rest = rest.split('"', 1)[0]
        currencies = [
            tok for tok in re.split(r"[,\s]+", rest.strip()) if _CURRENCY_RE.match(tok)
        ]
        out[account] = currencies
    return out


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    """One thing the agent read off the receipt, and how sure it was."""

    value: str
    confidence: float


def _field(value: str | None, confidence: float | None) -> Field:
    try:
        conf = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    return Field((value or "").strip(), max(0.0, min(conf, 1.0)))


def _judge(name: str, field: Field, floor: float) -> dict:
    """Render one field for the response — value, or a flag and never a value.

    A below-floor field returns `value: None`. Not the value with a warning
    beside it: a value in a response is something a caller will use, and #516's
    whole point is that a caller which ignores confidence must still be unable
    to produce a confident wrong entry.
    """
    if not field.value:
        return {"value": None, "confidence": field.confidence, "flag": "!",
                "reason": f"nothing was read for {name}"}
    if field.confidence < floor:
        return {"value": None, "confidence": field.confidence, "flag": "!",
                "reason": f"{name} was read at {field.confidence:.2f}, "
                          f"below the {floor:.2f} floor"}
    return {"value": field.value, "confidence": field.confidence, "flag": None}


def _amount(text: str) -> Decimal | None:
    """A decimal from what the agent typed, or None.

    Tolerant of the shapes that come off a receipt (`$12.34`, `12,34`, `12.34 EUR`,
    `1,234.56`) and refuses everything else rather than guessing — an amount we
    cannot parse is a below-floor amount by another route.
    """
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.,\-]", "", text.strip())
    if not cleaned:
        return None
    # `1.234,56` (European) vs `1,234.56`: the LAST separator is the decimal one.
    last_dot, last_comma = cleaned.rfind("."), cleaned.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        if last_comma > last_dot:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif last_comma >= 0:
        # A lone comma is a decimal separator when it leaves two digits behind
        # ("12,34") and a thousands separator otherwise ("1,234").
        cleaned = (
            cleaned.replace(",", ".")
            if len(cleaned) - last_comma - 1 == 2
            else cleaned.replace(",", "")
        )
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso_date(text: str) -> str | None:
    if not _ISO_DATE.match(text or ""):
        return None
    try:
        _date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _quote(text: str) -> str:
    """Beancount string literal. Newlines and quotes both break the directive."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


_MERCHANT_SAFE = re.compile(r"[^\w &'./,()+-]", re.UNICODE)


def _clean_merchant(name: str) -> str:
    return _MERCHANT_SAFE.sub(" ", name).strip()[:120]


# ---------------------------------------------------------------------------
# The proposal
# ---------------------------------------------------------------------------
def build_proposal(
    *,
    receipt: dict,
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
    line_items: list[dict] | None = None,
    extracted_text: str = "",
    expense_account: str = "",
    paid_from_account: str = "",
    operating_currency: str = "",
    exchange_rate: str = "",
    account_currencies: dict[str, list[str]] | None = None,
    capture_date: str = "",
    confidence_floor: float | None = None,
) -> dict:
    """Turn one reading of one receipt into a proposal. Never writes."""
    floor = effective_floor(confidence_floor)
    # `None` and `{}` mean different things and conflating them would be the
    # expensive direction: `{}` is "this book has no open accounts", which must
    # block, while `None` is "nobody looked", which must not pretend to have.
    constraints = account_currencies
    capture = _iso_date(capture_date) or _date.today().isoformat()

    fields = {
        "date": _judge("the date", _field(date, date_confidence), floor),
        "merchant": _judge("the merchant", _field(merchant, merchant_confidence), floor),
        "total": _judge("the total", _field(total, total_confidence), floor),
        "tax": _judge("the tax", _field(tax, tax_confidence), floor),
        "currency": _judge("the currency", _field(currency, currency_confidence), floor),
    }

    warnings: list[dict] = []
    blockers: list[dict] = []
    notes: list[str] = []

    # --- date -------------------------------------------------------------
    # Below the floor, this falls back to the CAPTURE date and says so, rather
    # than blocking. A receipt is normally photographed within a day or two of
    # being issued, so the capture date is a defensible approximation and the
    # `!` plus the `date-uncertain` metadata make it reviewable. This is the one
    # substitution in the whole function, and it exists because a directive
    # cannot be written without a date at all.
    txn_date = _iso_date(fields["date"]["value"] or "")
    date_assumed = False
    if txn_date is None:
        txn_date = capture
        date_assumed = True
        if fields["date"]["value"]:
            fields["date"] = {
                "value": None, "confidence": fields["date"]["confidence"], "flag": "!",
                "reason": f"{date!r} is not an ISO date (YYYY-MM-DD)",
            }
        warnings.append({
            "code": "date_assumed",
            "message": f"The date was not legible, so the capture date {capture} is "
                       f"used and the entry is flagged `!`. Correct it before "
                       f"approving if the receipt is older than that.",
        })

    # --- currency ---------------------------------------------------------
    ccy = (fields["currency"]["value"] or "").upper()
    if not _CURRENCY_RE.match(ccy or ""):
        ccy = ""
    if not ccy and operating_currency:
        ccy = operating_currency.strip().upper()
        warnings.append({
            "code": "currency_assumed",
            "message": f"The currency was not legible, so the book's operating "
                       f"currency {ccy} is assumed and the entry is flagged `!`. "
                       f"A receipt from a trip is the case this gets wrong.",
        })
    if not ccy:
        blockers.append({
            "code": "currency_unknown",
            "message": "The currency was not legible and no operating currency "
                       "was supplied, so there is nothing to denominate the "
                       "amount in. Pass the book's currency as a hint, or read "
                       "the receipt again.",
        })

    # --- total ------------------------------------------------------------
    # 🔴 NEVER SUBSTITUTED AND NEVER GUESSED. Every other field has a defensible
    # fallback; the amount does not. A wrong total is the exact harm #516 exists
    # to prevent, and it survives every downstream check because the entry still
    # balances.
    total_amount = _amount(fields["total"]["value"] or "")
    if total_amount is None:
        blockers.append({
            "code": "total_not_legible",
            "message": "The total was not read above the confidence floor, so "
                       "there is no amount to propose. Nothing is guessed here: "
                       "a wrong total balances just as well as a right one and "
                       "nothing downstream would ever catch it.",
        })

    # --- tax --------------------------------------------------------------
    tax_amount = _amount(fields["tax"]["value"] or "")
    if tax_amount is not None and total_amount is not None and tax_amount >= total_amount:
        warnings.append({
            "code": "tax_not_below_total",
            "message": f"The tax read as {tax_amount} against a total of "
                       f"{total_amount}, so one of them is not what it looks "
                       f"like. The tax is dropped from the proposal.",
        })
        tax_amount = None
        fields["tax"] = {"value": None, "confidence": fields["tax"]["confidence"],
                         "flag": "!", "reason": "tax was not below the total"}

    # --- line items -------------------------------------------------------
    kept: list[dict] = []
    dropped = 0
    for item in line_items or []:
        desc = str(item.get("description", "")).strip()
        amt = _amount(str(item.get("amount", "")))
        try:
            conf = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        if not desc or amt is None or conf < floor:
            dropped += 1
            continue
        kept.append({"description": desc[:80], "amount": str(amt), "confidence": conf})

    # THE "LARGEST NUMBER ON THE PAGE IS NOT THE TOTAL" CHECK.
    #
    # A receipt carries a subtotal, a tax line, a tender, a change line and
    # sometimes a loyalty balance, and the total is not reliably the biggest of
    # them. This is the one cross-field check available without re-reading the
    # image, and it is worth a `!` rather than a refusal because a legitimate
    # discount also breaks it.
    if total_amount is not None and kept:
        biggest = max(Decimal(i["amount"]) for i in kept)
        if biggest > total_amount:
            warnings.append({
                "code": "line_item_exceeds_total",
                "message": f"A line item reads {biggest} against a total of "
                           f"{total_amount}. On a receipt the largest number on "
                           f"the page is often a tender or a subtotal rather "
                           f"than the total — check which is which.",
            })
        summed = sum(Decimal(i["amount"]) for i in kept) + (tax_amount or Decimal(0))
        if abs(summed - total_amount) > Decimal("0.01"):
            warnings.append({
                "code": "total_does_not_reconcile",
                "message": f"The legible line items plus tax come to {summed}, "
                           f"and the total reads {total_amount}. A discount or "
                           f"an illegible line explains this often enough that "
                           f"it is not a refusal, but it is not nothing.",
            })

    # --- accounts, and the two failures that would otherwise land at the write
    expense_account = expense_account.strip()
    paid_from_account = paid_from_account.strip()

    if not expense_account:
        blockers.append({
            "code": "expense_account_missing",
            "message": "No expense account was named. Categorisation is the "
                       "model's job — it is the part of this a model is "
                       "genuinely good at — and inventing an account here is "
                       "how a book acquires an `Expenses:Misc` nobody chose.",
        })
    if not paid_from_account:
        blockers.append({
            "code": "paid_from_missing",
            "message": "No account was named for what paid. A one-legged "
                       "transaction cannot be written, so this is proposed but "
                       "not write-ready. Ask which card or account it was.",
        })

    rate = _amount(exchange_rate)
    paid_ccy = ccy
    if constraints is None:
        warnings.append({
            "code": "accounts_not_checked",
            "message": "The book's `open` directives were not read, so neither "
                       "'is this account open' nor 'does it admit this "
                       "currency' has been checked. Both fail at the write, "
                       "after the person has approved the entry.",
        })
    for account, role in (
        () if constraints is None
        else ((expense_account, "expense"), (paid_from_account, "paid_from"))
    ):
        if not account:
            continue
        if account not in constraints:
            blockers.append({
                "code": "account_not_open",
                "account": account,
                "message": f"{account} has never been opened in this book, so a "
                           f"write would fail bean-check. Open it first "
                           f"(`open_accounts`) — and open it in a currency that "
                           f"admits {ccy or 'this receipt'}, because an `open` "
                           f"cannot be amended afterwards.",
            })
            continue
        pinned = constraints[account]
        if not ccy or not pinned or ccy in pinned:
            continue
        if role == "expense":
            blockers.append({
                "code": "currency_pinned",
                "account": account,
                "pinned_to": pinned,
                "message": f"{account} was opened as `open {account} "
                           f"{','.join(pinned)}` and this receipt is in {ccy}. "
                           f"bean-check refuses that with \"Invalid currency\", "
                           f"and NOTHING IN THE PRODUCT AMENDS AN EXISTING "
                           f"`open` — a second one for the same account is "
                           f"itself an error. Post it to a currency-specific "
                           f"sub-account instead (e.g. {account}:{ccy}), which "
                           f"is a new `open` rather than an edit.",
            })
        else:
            # The paying account is in another currency. That is ordinary and
            # Beancount handles it — with a PRICE. What it cannot do is invent
            # one, and neither can this: a rate needs a date and a source, and
            # there is no rate source anywhere in this product.
            if rate is not None and rate > 0 and pinned:
                paid_ccy = pinned[0]
            else:
                blockers.append({
                    "code": "needs_exchange_rate",
                    "account": paid_from_account,
                    "pinned_to": pinned,
                    "message": f"The receipt is in {ccy} and {paid_from_account} "
                               f"is pinned to {','.join(pinned)}, so the entry "
                               f"needs a price. No rate was supplied and none is "
                               f"invented here — a rate has a date and a source, "
                               f"and this product has neither. Pass "
                               f"`exchange_rate` (how much one {ccy} cost in "
                               f"{pinned[0]}) taken from the card statement, "
                               f"which is the rate you were actually charged.",
                })

    # --- the flag ---------------------------------------------------------
    # `!` whenever anything at all is uncertain, and `*` only when nothing is.
    # This is the property that makes a careless caller safe: the directive it
    # pastes into `add_transactions` lands as a PENDING entry that shows up in
    # Fava's "uncleared" view rather than as a settled one.
    uncertain = (
        any(f["flag"] == "!" for f in fields.values()) or bool(warnings) or dropped > 0
    )
    flag = "!" if uncertain else "*"

    write_ready = not blockers
    beancount = (
        _render(
            txn_date=txn_date,
            flag=flag,
            merchant=fields["merchant"]["value"],
            expense_account=expense_account,
            paid_from_account=paid_from_account,
            total=total_amount,
            ccy=ccy,
            tax=tax_amount,
            rate=rate if paid_ccy != ccy else None,
            paid_ccy=paid_ccy,
            receipt=receipt,
            date_assumed=date_assumed,
            line_items=kept,
        )
        if write_ready and total_amount is not None
        else None
    )

    if dropped:
        notes.append(
            f"{dropped} line item(s) were read below the floor and are omitted "
            f"rather than guessed; the total is unaffected."
        )
    notes.append(
        "This is a proposal. This tool does not write and cannot be made to — "
        "`add_transactions` is the only write path, and it is the one that runs "
        "bean-check and commits."
    )

    return {
        "receipt": receipt,
        "confidence_floor": floor,
        "fields": fields,
        "line_items": kept,
        "dropped_line_items": dropped,
        "extracted_text": extracted_text,
        "transaction_flag": flag,
        "write_ready": write_ready,
        "blockers": blockers,
        "warnings": warnings,
        "notes": notes,
        "beancount": beancount,
        "next_step": (
            "Show the person the merchant, date and total, and the flagged "
            "fields as questions rather than as facts. Only after they confirm, "
            "pass `beancount` to `add_transactions` unchanged."
            if write_ready
            else "Do not write anything. Resolve every blocker above first — "
                 "each one is a bean-check failure that would otherwise land "
                 "AFTER the person approved the entry."
        ),
    }


def _render(
    *,
    txn_date: str,
    flag: str,
    merchant: str | None,
    expense_account: str,
    paid_from_account: str,
    total: Decimal,
    ccy: str,
    tax: Decimal | None,
    rate: Decimal | None,
    paid_ccy: str,
    receipt: dict,
    date_assumed: bool,
    line_items: list[dict],
) -> str:
    """The directive itself.

    The receipt reference is TRANSACTION metadata rather than posting metadata,
    because the evidence is for the whole entry — and because a posting-level
    key would be duplicated on the balancing leg, which is how a `git diff` of a
    ledger stops being readable.
    """
    payee = _quote(_clean_merchant(merchant)) + " " if merchant else ""
    narration = _quote("Receipt" if merchant else "Receipt — merchant not legible")
    lines = [f"{txn_date} {flag} {payee}{narration}"]

    key = receipt.get("key") or ""
    sha = receipt.get("sha256") or ""
    if key:
        lines.append(f"  receipt-key: {_quote(key)}")
    if sha:
        lines.append(f"  receipt-sha256: {_quote(sha)}")
    storage = receipt.get("storage") or ""
    if storage:
        lines.append(f"  receipt-storage: {_quote(storage)}")
    if date_assumed:
        lines.append("  date-uncertain: TRUE")

    for item in line_items:
        lines.append(f"  ; {item['description']}  {item['amount']} {ccy}")

    posting = f"  {expense_account}  {total} {ccy}"
    if rate is not None:
        # `@` is the PER-UNIT price, which is what a card statement gives you.
        posting += f" @ {rate} {paid_ccy}"
    lines.append(posting)
    if tax is not None:
        # Metadata, not a second posting: a `Expenses:Taxes:…` posting needs an
        # account that may not exist, and inventing one is the failure mode two
        # blockers above already exist to prevent.
        lines.append(f"    tax: {tax} {ccy}")
    lines.append(f"  {paid_from_account}")
    return "\n".join(lines) + "\n"
