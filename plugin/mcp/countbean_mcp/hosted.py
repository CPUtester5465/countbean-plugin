"""Talk to a customer's HOSTED book over the frozen sidecar API (#37).

Why this file exists
--------------------
Before it, ``server.py`` opened ``~/.countbean/main`` and edited it directly.
The failure mode was not "hosted mode is missing" — it is that the plugin
**succeeded**: a customer running Claude with our plugin got a commit hash back
and a plausible net worth, from a ledger we do not host, back up, or serve in
Fava. Nothing errored, so nothing prompted them to look.

This is the client half of CONTRACTS §4. The server half is frozen and
unchanged: ``POST /transactions`` and ``/accounts`` need ``readwrite``;
``/ledger``, ``/query``, ``/balances``, ``/report``, ``/history`` need ``read``.
Caddy inside the machine strips the ``/api`` prefix, so the paths below are
``/api/<route>``.

Auth is ONE hop from the laptop. The control-plane proxy
(``ANY /v1/books/:bookId/api/*``, CONTRACTS §3, ``contract/v3``) authenticates
the per-book API key on every request and mints the book-scoped JWT itself,
inside our network. The JWT never reaches a customer's machine.

That was ruled over the alternative — client mints, client caches — for three
reasons worth keeping here, because each one is a defect this file would
otherwise have:

  * **Revocation would not work** (#50). A cached JWT keeps ``readwrite`` for up
    to 61 minutes after the key is revoked, on a laptop, with no way to recall
    it. Per-request key checks make "revoke" mean what the UI says.
  * **A fleet re-secret would break every client.** Cached tokens are signed
    under whichever secret was current; rotating machines would fail signature
    verification everywhere and look like "Claude stopped working".
  * ``token_mint`` is metered with a hard cap that answers 429, so a mint per
    tool call gets slower and then breaks under exactly the usage we want.

Deliberately stdlib-only (``urllib``): this package is installed on a
customer's laptop by ``run.sh``, and a new dependency there is a new way for a
first run to fail. The requests are small and synchronous; MCP tools are too.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import credentials
from .ledger import CommitResult, LedgerError

_DEFAULT_TIMEOUT = 30.0


class HostedConfigError(LedgerError):
    """Hosted mode is half-configured — say which part, not "unauthorized"."""


class HostedNoBookError(LedgerError):
    """§4 `no_book`: this runtime genuinely has no ledger provisioned.

    The ONLY failure that means absence. Everything else — a bad key, a wrong
    book, a waking machine, a quota cap — is a book that exists and could not
    be reached, and saying "no book yet" about any of them tells a customer
    their data is gone at a moment when it is not.
    """


class HostedUnavailableError(LedgerError):
    """The book is real and ours, but not answering yet — waking, or wedged.

    Its own type because the proxy answers 409 for several different situations
    (§3.9: no runtime, unavailable, starting up, unreachable, a silent app
    behind a live front) while the runtime answers 409 only for ``no_book``.
    Folding them together would make a book that is merely booting look like a
    book that does not exist.

    **What every member of this type has in common is that RETRYING IS THE
    RIGHT RESPONSE**, and callers rely on it: ``server._confirm_reachable``
    sleeps and re-calls on this type, six times. That is the property to check
    before filing a new 409 here — not the count. This docstring claimed FOUR
    conditions from ``contract/v2`` until #365; §3.9 named three and the route
    had five, and neither number was ever load-bearing. A permanent condition
    filed here is a caller looping forever, which is why ``book_paused`` is
    ``HostedPausedError`` and not this.
    """


class HostedPausedError(LedgerError):
    """§3.9 ``book_paused``: the book row says its machine must stay stopped.

    ``contract/v3``. The ONE 409 from the proxy that does not clear on its own —
    an owner's Suspend, an operator's, or ``cancelSubscription`` suspending
    every book in a cancelled org. Nothing is deleted and the ledger is intact;
    the machine is simply not allowed to run, so the proxy refuses instead of
    starting it.

    ⚠️ NOT a subclass of ``HostedUnavailableError``, and that is the whole point
    of the class rather than a taxonomy preference: ``_confirm_reachable``
    catches that type and sleeps, so inheriting would put a permanent condition
    back inside the 30-second retry loop this type exists to stay out of.

    The way out is the web app — ``POST /v1/books/:bookId/resume`` needs a user
    session and refuses an api-key — so the control-plane's own message is
    surfaced rather than replaced.
    """


class HostedQuotaError(LedgerError):
    """The usage cap was hit. The book is fine; we are not being let through."""


class HostedAccessError(LedgerError):
    """We could not reach THIS book: bad key, wrong book, or no access.

    Separate from a plain ``LedgerError`` because ``exists()`` must not fold it
    into "no book yet". Told that, a customer is invited to create a book —
    which is refused in hosted mode, and which was never the problem.
    """


@dataclass
class HostedConfig:
    control_url: str
    api_key: str
    book_id: str
    book_url: str = ""
    timeout: float = _DEFAULT_TIMEOUT

    @property
    def api_base(self) -> str:
        """Where this book's §4 surface lives.

        Normally the control-plane proxy, derived from the control URL and the
        book id so the two cannot disagree — a customer cannot point
        ``BOOK_URL`` at book A while ``BOOK_ID`` says B.

        ``COUNTBEAN_BOOK_URL`` overrides it for talking to a runtime directly
        (local development). Both forms end in ``/api``: the proxy route is
        ``/v1/books/:bookId/api/*`` and Caddy strips the same prefix inside the
        machine, so the paths below are identical either way.
        """
        if self.book_url:
            return self.book_url.rstrip("/") + "/api"
        return self.control_book_base + "/api"

    @property
    def control_book_base(self) -> str:
        """`…/v1/books/<id>` on the CONTROL PLANE, without the `/api` suffix.

        Everything in §4 hangs off `/api`, because Caddy strips that prefix
        inside the machine and the runtime serves the rest. Receipt evidence
        (§3.12, #516) does not: the bytes go to an object bucket whose
        credentials live in the control-plane and are injected into no book
        machine, so the route is a control-plane route and sits one level up.

        Deriving it from `control_url` and `book_id` — rather than accepting one
        — is the same rule `api_base` follows: a client cannot address book A
        while claiming to be book B.
        """
        book = urllib.parse.quote(self.book_id, safe="")
        return f"{self.control_url.rstrip('/')}/v1/books/{book}"


def config_from_env(env: dict[str, str] | None = None) -> HostedConfig | None:
    """Read hosted config, or None for local mode.

    ``COUNTBEAN_API_KEY`` is the switch. Once it is set the caller has asked for
    the hosted book, so a missing companion value is an error rather than a
    silent fall back to the laptop — falling back is the exact defect #37 is
    about, and it would be invisible again.

    With no argument the three sources in ``credentials.py`` are consulted:
    process environment, then a ``.env``, then the saved connection written by
    ``connect_book``. Called per tool call (``server.py:_book``), so connecting
    takes effect on the next call rather than the next launch.

    An EXPLICIT dict is used exactly as given and consults nothing else. Callers
    that pass one are asking about a specific environment — a test, or a
    diagnostic — and quietly folding a saved file into their answer would make
    this function untestable and its result unattributable.
    """
    env = credentials.resolve() if env is None else env
    api_key = (env.get("COUNTBEAN_API_KEY") or "").strip()
    if not api_key:
        return None

    book_id = (env.get("COUNTBEAN_BOOK_ID") or "").strip()
    book_url = (env.get("COUNTBEAN_BOOK_URL") or "").strip()
    control_url = (env.get("COUNTBEAN_CONTROL_URL") or "https://api.countbean.com").strip()

    # BOOK_URL is optional now — the proxy address is derived. BOOK_ID is not:
    # without it there is nothing to address and nothing to check health against.
    missing = [name for name, value in (("COUNTBEAN_BOOK_ID", book_id),) if not value]
    if missing:
        raise HostedConfigError(
            "COUNTBEAN_API_KEY is set, so this plugin is configured for a hosted "
            "book, but " + " and ".join(missing) + " is missing. Refusing to fall "
            "back to a local ledger: that would silently edit a different book "
            "from the one you are paying us to keep."
        )
    return HostedConfig(
        control_url=control_url, api_key=api_key, book_id=book_id, book_url=book_url
    )


class HostedBook:
    """A book that lives on our infrastructure, addressed over HTTP.

    Mirrors the subset of ``Ledger`` that the MCP tools call, so ``server.py``
    branches in one place. It is NOT a Ledger subclass on purpose: the local
    class shells out to git and bean-check, and inheriting would make a missed
    override fall through to editing a laptop directory — the failure this
    module exists to remove.
    """

    def __init__(self, cfg: HostedConfig):
        self.cfg = cfg

    # ---- identity ---------------------------------------------------------
    @property
    def root(self) -> str:
        """What to show a human. Never a local path in hosted mode."""
        return f"{self.cfg.api_base} (book {self.cfg.book_id})"

    def exists(self) -> bool:
        """``GET /api/health`` — and the cheapest check we reached the RIGHT book.

        Health returns ``book_id``; a mismatch means this address answered for
        somebody else's machine, so it is checked before anything is written.

        Sent WITH the API key even though §4 row 1 makes health unauthenticated
        on the runtime: the proxy in front checks the key on every request so
        that revoking one takes effect immediately (#50). An unauthenticated
        call would simply 401 there, and the guard would never run.
        """
        # Only ONE failure means absence, and it is named. Everything else
        # propagates.
        #
        # This was an allow-list of errors to re-raise, and 429 was not on it —
        # so hitting the usage cap reported "No book yet. Call create_book",
        # the exact sentence three earlier fixes existed to remove. Enumerating
        # the exceptions means every status added later is absence by default,
        # and wrong by default. Enumerating absence inverts that: a status
        # nobody has thought about surfaces its own message instead of claiming
        # the customer has no book.
        try:
            body = self._request("GET", "/health")
        except HostedNoBookError:
            return False
        served = body.get("book_id")
        if served and served != self.cfg.book_id:
            raise LedgerError(
                f"{self.cfg.api_base} answered for book {served}, not "
                f"{self.cfg.book_id}. Do not write to it: this address is not "
                f"routing to your book."
            )
        return True

    def init(self, name: str = "", currency: str = "") -> CommitResult:
        raise LedgerError(
            "A hosted book is created when you buy it, not from the plugin. "
            "This plugin is connected to an existing book; there is nothing to "
            "initialise."
        )

    # ---- reads ------------------------------------------------------------
    def read_all(self) -> str:
        return self._request("GET", "/ledger")["text"]

    def query(self, bql: str) -> list[dict[str, str]]:
        return self._request("POST", "/query", {"bql": bql})["rows"]

    def balances(self, account_filter: str = "") -> list[dict[str, str]]:
        path = "/balances"
        if account_filter:
            path += "?" + urllib.parse.urlencode({"account_filter": account_filter})
        return self._request("GET", path)["rows"]

    def accounts(self) -> list[str]:
        """No §4 route lists accounts, so ask BQL for them.

        The contract says a convenience ``GET /accounts`` MAY exist but is not
        part of the frozen nine, so relying on it would make this plugin need a
        route the contract does not promise.
        """
        rows = self.query("SELECT DISTINCT account ORDER BY account")
        out = []
        for row in rows:
            value = row.get("account") or next(iter(row.values()), "")
            if value:
                out.append(value)
        return out

    def history(self, n: int = 20) -> list[dict[str, str]]:
        return self._request("GET", f"/history?limit={int(n)}")["data"]

    def report_bytes(self, body: dict) -> bytes:
        """``POST /api/report`` — returns rendered bytes, not JSON.

        Kept off ``_request`` on purpose: that path decodes UTF-8 and parses
        JSON, and an xlsx workbook is neither. Decoding it would corrupt the
        file in a way that only shows up when the customer opens it.
        """
        req = urllib.request.Request(
            self.cfg.api_base + "/report", data=json.dumps(body).encode(), method="POST"
        )
        req.add_header("Authorization", f"Bearer {self._bearer()}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise _from_http_error(exc, "generate a report") from None
        except urllib.error.URLError as exc:
            raise LedgerError(f"Could not reach the hosted book: {exc.reason}") from None

    # ---- receipt evidence (#516, §3.12) -----------------------------------
    def store_receipt(self, content_base64: str, content_type: str, sha256: str) -> dict:
        """Put a receipt in the book's folder and get its reference back.

        ⚠️ NO KEY IS SENT AND NONE COULD BE. The control-plane derives
        `receipts/<orgId>/<bookId>/<sha256>` from the book this api-key already
        authenticates for; the request body carries bytes, a content type and
        our own hash of those bytes, and nothing else. That is deliberate — a
        caller-supplied path would be a cross-tenant read, which is the same
        property `from.id` carries under the shared Telegram bot (#518).
        The `sha256` we send can only cause a REFUSAL (it is compared against
        theirs), never a placement.

        Refused rather than degraded when the plugin is pointed straight at a
        runtime with `COUNTBEAN_BOOK_URL`: bucket credentials are never injected
        into a book machine, so there is no bucket at the far end of that path.
        """
        if self.cfg.book_url:
            raise LedgerError(
                "COUNTBEAN_BOOK_URL points this plugin straight at a book "
                "runtime, and receipt storage is a control-plane route — the "
                "bucket's credentials are never injected into a book machine. "
                "Unset COUNTBEAN_BOOK_URL to store receipts."
            )
        return self._request_url(
            "POST",
            self.cfg.control_book_base + "/receipts",
            {
                "content_base64": content_base64,
                "content_type": content_type,
                "sha256": sha256,
            },
        )

    # ---- writes -----------------------------------------------------------
    def add_directives(self, text: str, message: str = "") -> CommitResult:
        return self._commit("/transactions", {"beancount_text": text})

    def open_accounts(self, text: str) -> CommitResult:
        return self._commit("/accounts", {"beancount_text": text})

    def revert(self, ref: str) -> CommitResult:
        return self._commit("/revert", {"commit": ref})

    def _commit(self, path: str, body: dict) -> CommitResult:
        data = self._request("POST", path, body)
        return CommitResult(commit=data["commit"], message=data["message"])

    # ---- auth -------------------------------------------------------------
    def _bearer(self) -> str:
        """The per-book API key, sent as-is.

        There is deliberately no token minting here. The proxy authenticates
        this key on every request and mints the runtime JWT server-side; a JWT
        cached on a laptop is what would keep working after a revoke and break
        after a rotation. See the module docstring.
        """
        return self.cfg.api_key

    # ---- transport --------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None):
        return self._request_url(method, self.cfg.api_base + path, body, what=f"{method} {path}")

    def _request_url(
        self, method: str, url: str, body: dict | None = None, what: str = ""
    ):
        """Same transport, an ABSOLUTE url.

        Exists because §3.12 is a control-plane route rather than a §4 runtime
        one, so it does not hang off `api_base`. Kept as one function so a
        header, a timeout or an error mapping cannot be added to one path and
        forgotten on the other.
        """
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._bearer()}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        return self._send(req, what=what or f"{method} {urllib.parse.urlparse(url).path}")

    def _send(self, req: urllib.request.Request, what: str) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise _from_http_error(exc, what) from None
        except urllib.error.URLError as exc:
            raise LedgerError(
                f"Could not reach the hosted book to {what}: {exc.reason}. "
                f"A book scales to zero when idle, so the first call after a "
                f"quiet period can take a few seconds — try once more."
            ) from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # /report returns HTML or xlsx, which callers handle separately.
            return {"raw": raw}


def _from_http_error(exc: urllib.error.HTTPError, what: str) -> LedgerError:
    """Turn the contract's error envelope back into a message worth reading.

    §4 defines ``{"error": {"code", "message"}}``. A 422 ``ledger_rejected``
    carries the validator's own text, which is the single most useful thing we
    can hand back to an agent composing beancount — so it is surfaced verbatim
    rather than replaced with a status code.
    """
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        err = payload.get("error") or {}
        code = err.get("code") or ""
        message = err.get("message") or ""
    except Exception:
        code, message = "", ""

    if exc.code == 422:
        # Same two-meanings-one-status shape as 409, one code earlier.
        # `ledger_rejected` is §4: bean-check refused the directives and the
        # book is unchanged. `validation` is the PROXY refusing to forward the
        # path at all (§3.9 `safeRuntimePath`) — nothing reached a ledger, so
        # reporting it as a rejection would send an agent off rewriting
        # beancount that was never the problem.
        if code == "validation":
            return HostedUnavailableError(
                f"The control plane would not forward that request: {message} "
                f"This is a client bug, not a problem with your book — the "
                f"ledger was not touched."
            )
        return LedgerError(message or "The book rejected this change.")
    if exc.code == 401:
        return HostedAccessError(
            f"The hosted book rejected our credentials ({code or 'unauthenticated'}). "
            f"Check COUNTBEAN_API_KEY and COUNTBEAN_BOOK_ID. {message}".strip()
        )
    if exc.code == 403:
        return HostedAccessError(
            "This API key is read-only, so it cannot change the book. "
            "A read key is the safe default; ask for a readwrite key if you "
            "want the agent to post entries."
        )
    if exc.code == 404:
        # The proxy answers 404 both for "no such book" and for "not yours"
        # (§3.9, 404-not-403), deliberately, so it cannot be used to discover
        # which book ids exist. So this message must not guess which it was.
        return HostedAccessError(
            "That book was not found, or this API key does not have access to "
            "it. Check COUNTBEAN_BOOK_ID against the book the key was issued "
            "for — the two are checked together and the server will not say "
            "which one is wrong."
        )
    if exc.code == 409:
        # Three very different meanings on one status, told apart by code.
        # `no_book` is §4: the runtime has no ledger provisioned. `book_paused`
        # is §3.9 and is the only one that will never clear by itself. Anything
        # else is the PROXY — most often "starting up", which is the normal
        # first call after a book has scaled to zero.
        if code == "no_book":
            return HostedNoBookError(message or "That book has no ledger yet.")
        if code == "book_paused":
            # The control-plane's sentence, not ours: it is the one that knows
            # the book is paused rather than merely quiet, and §3.4 already
            # requires it to name the way out. A default is kept because an
            # older control-plane could send the code with an empty envelope,
            # and "retry shortly" is the one thing this must never say.
            return HostedPausedError(
                message
                or "This book is paused, so its machine is stopped and "
                "retrying will not start it. Nothing has been deleted. "
                "Resume the book from its page in the web app."
            )
        return HostedUnavailableError(
            message
            or "This book is not answering yet. A book sleeps when idle and "
            "takes a few seconds to wake — try again shortly."
        )
    if exc.code in (502, 503, 504):
        # MEASURED on a real cold start, 2026-08-08: a parked book answered
        # 409 "starting up", then 502 five seconds later, then 200. The gateway
        # is talking to a machine that is booting and not yet listening.
        #
        # The structural fix already stopped this reading as absence — 502 has
        # no branch, so it propagated instead of becoming "no book yet", which
        # is exactly what the 500 test was written for. What it did NOT have
        # was useful advice, and mid-cold-start is precisely when a customer
        # needs "wait" rather than a status code.
        return HostedUnavailableError(
            "The book is still starting up (the gateway could not reach it "
            "yet). This is normal on the first call after a book has been "
            "idle — try again in a few seconds."
        )
    if exc.code == 429:
        # Quota, not absence, and NOT time-based: the control-plane meter
        # clears on reconcile or a process restart, so "retry shortly" would
        # send a customer into an infinite retry. Say what is true and do not
        # promise when it lifts, because nothing currently schedules that.
        return HostedQuotaError(
            message
            or "This book has hit its usage cap on the control plane. Further "
            "requests will be refused until the cap is raised or reset."
        )
    return LedgerError(f"Failed to {what}: HTTP {exc.code} {code} {message}".strip())

# --------------------------------------------------------------- device grant -
# RFC 8628. The plugin asks for access; a signed-in human approves it in a
# browser; the plugin collects a per-book API key.
#
# Why this exists next to `connect_book` rather than replacing it: pasting a key
# still has to work. It is what a script does, what CI does, and what somebody
# does when the browser is on a different machine from the terminal. The device
# grant is the path for a person, not the only path.

DEVICE_AUTHORIZE_PATH = "/v1/device/authorize"
DEVICE_TOKEN_PATH = "/v1/device/token"


class DeviceFlowError(LedgerError):
    """The grant did not complete. Carries a reason a person can act on."""


class DevicePending(Exception):
    """Not an error: nobody has approved it yet. Internal to the poll loop."""


def _post_json(url: str, body: dict, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            message = (payload.get("error") or {}).get("message") or ""
        except Exception:
            message = ""
        # 428 is this server's "authorization_pending" and 429 its "slow_down".
        # Both are the loop working as designed, not failures — raising a plain
        # LedgerError here would abort a grant that is one human click away.
        if exc.code in (428, 429):
            raise DevicePending(message) from None
        raise DeviceFlowError(message or f"HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise DeviceFlowError(f"Could not reach Countbean: {exc.reason}") from None
    return json.loads(raw) if raw else {}


def start_device_authorization(control_url: str) -> dict:
    """Begin a grant. Returns the code to show the human, and how to poll."""
    return _post_json(control_url.rstrip("/") + DEVICE_AUTHORIZE_PATH, {})


def poll_device_authorization(
    control_url: str,
    device_code: str,
    interval: float,
    expires_in: float,
    sleep=None,
) -> dict:
    """Poll until approved, declined, or expired.

    `sleep` is injectable so tests do not spend real seconds. The interval is
    obeyed and BACKED OFF on `slow_down`: the server throttles per row, so a
    client that ignores it makes its own grant slower, not faster.
    """
    sleeper = sleep or time.sleep
    url = control_url.rstrip("/") + DEVICE_TOKEN_PATH
    deadline = time.monotonic() + expires_in
    wait = max(1.0, float(interval))

    while time.monotonic() < deadline:
        sleeper(wait)
        try:
            return _post_json(url, {"device_code": device_code})
        except DevicePending as pending:
            if "slow_down" in str(pending):
                wait += 5.0
            continue

    raise DeviceFlowError(
        "The request expired before it was approved. Nothing was connected — "
        "run it again to get a fresh code."
    )

