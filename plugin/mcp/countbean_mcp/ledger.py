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
from contextlib import contextmanager
from dataclasses import dataclass
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

# One lock object per book root, shared by every Ledger instance in the process.
# Ledger is constructed per request, so the lock cannot live on the instance.
_ROOT_LOCKS: dict[Path, threading.Lock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(root: Path) -> threading.Lock:
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(root, threading.Lock())


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
        self.main.write_text(
            f'option "title" "{name}"\n'
            f'option "operating_currency" "{currency}"\n\n'
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
        (self.root / ".gitignore").write_text("*.xlsx\n*.html\n__pycache__/\n")

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
        """Run bean-check; raise LedgerError with details on failure."""
        proc = self._run("bean-check", str(self.main), check=False)
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

        routed = self._route(text)
        with self.write_lock():
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
        self, *args: str, cwd: str | os.PathLike | None = None, check: bool = True
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
