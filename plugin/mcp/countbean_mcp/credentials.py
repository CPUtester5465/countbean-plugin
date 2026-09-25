"""Where a hosted book's coordinates come from, and how they get there.

WHY THIS FILE EXISTS
--------------------
Connecting the plugin used to require exporting two shell variables *before*
starting Claude Code:

    export COUNTBEAN_API_KEY='cbk_…'
    export COUNTBEAN_BOOK_ID='bok_…'

That is three problems wearing one coat, and the reporter hit all three:

  * **It has to happen first.** The MCP server's environment is fixed when
    Claude Code launches it. A customer who discovers the recipe *while* Claude
    is running must quit, export, and relaunch — and nothing on screen says so,
    so the natural read of a failing tool call is "the product is broken".
  * **It is shell-specific and invisible.** `export` in one terminal does not
    reach the Claude Code launched from another, and a `~/.zshrc` edit is not
    something to ask a bookkeeper for.
  * **It leaves the key in shell history and the process table**, which is a
    worse place for it than a 0600 file.

So credentials are resolved on EVERY tool call (``server.py`` calls
``config_from_env()`` per call, not at import), from three sources in
precedence order:

    1. the process environment      — unchanged; still wins, still scriptable
    2. a ``.env`` file              — the reporter's stated preference, and
                                      what a developer expects to work
    3. ``~/.countbean/credentials.json`` — written by the ``connect_book``
                                      tool, i.e. by Claude, from one pasted line

Because resolution is per-call, ``connect_book`` takes effect IMMEDIATELY. No
restart, no relaunch, no shell. That is the whole point of the file: it is what
lets "paste one line into Claude" be the entire setup.

Only ``COUNTBEAN_*`` names are ever read out of a ``.env``. A ledger tool has no
business importing a customer's ``AWS_SECRET_ACCESS_KEY`` into its process
because it happened to be two lines further down.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Every name that means something to this plugin. An allow-list, not a prefix
# scan at use-time, so adding a setting is a deliberate edit here.
KNOWN_KEYS = (
    "COUNTBEAN_API_KEY",
    "COUNTBEAN_BOOK_ID",
    "COUNTBEAN_BOOK_URL",
    "COUNTBEAN_CONTROL_URL",
    "COUNTBEAN_BOOK",
)

# How far up from the working directory to look for a `.env`. Enough to find a
# repo root from a subdirectory; not so far that a stray file in $HOME silently
# reconfigures every book the customer owns.
_ENV_SEARCH_PARENTS = 4


def credentials_path() -> Path:
    """The saved-credentials file. Overridable for tests and for odd setups."""
    override = os.environ.get("COUNTBEAN_CREDENTIALS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".countbean" / "credentials.json"


# ------------------------------------------------------------------ .env -----
def _parse_env_text(text: str) -> dict[str, str]:
    """A deliberately small ``.env`` parser: ``KEY=value``, one per line.

    Handles the three things real ``.env`` files do — a leading ``export``,
    surrounding quotes, and ``#`` comments — and nothing else. It does NOT do
    variable interpolation or command substitution: this file is read by a
    process holding a customer's ledger credentials, and ``$(…)`` in a dotfile
    must never become something this plugin runs.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key not in KNOWN_KEYS:
            continue
        value = value.strip()
        # Strip ONE matched pair of quotes. An unmatched quote is left alone so
        # a malformed line is visibly wrong rather than quietly truncated.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # Only an UNQUOTED value can carry a trailing comment; inside quotes
            # a '#' is part of the secret.
            value = value.split(" #", 1)[0].rstrip()
        if value:
            out[key] = value
    return out


def env_file_path(start: Path | None = None) -> Path | None:
    """Locate the ``.env`` that applies here, or None.

    ``COUNTBEAN_ENV_FILE`` names one explicitly. Otherwise walk up from the
    working directory — the same rule every other tool with a ``.env`` uses, so
    it behaves the way a developer already expects.
    """
    explicit = os.environ.get("COUNTBEAN_ENV_FILE")
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None

    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents][: _ENV_SEARCH_PARENTS + 1]:
        path = candidate / ".env"
        if path.is_file():
            return path
    return None


def _read_env_file() -> dict[str, str]:
    path = env_file_path()
    if path is None:
        return {}
    try:
        return _parse_env_text(path.read_text(encoding="utf-8"))
    except OSError:
        # An unreadable .env is not worth failing a ledger read over; the other
        # two sources may well answer.
        return {}


# ------------------------------------------------------------ saved file -----
def _read_saved() -> dict[str, str]:
    path = credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(v) for k, v in data.items() if k in KNOWN_KEYS and v}


def save(api_key: str, book_id: str, control_url: str = "") -> Path:
    """Persist a connection. 0600, and 0700 on the directory.

    Written whole rather than merged: "connect me to this book" replaces the
    previous book outright. Merging would let a stale ``COUNTBEAN_BOOK_URL``
    from an earlier connection survive and re-point the new key at the old
    machine — a mismatch ``HostedBook.exists()`` would catch, but only after
    the customer had been told they were connected.
    """
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = {"COUNTBEAN_API_KEY": api_key, "COUNTBEAN_BOOK_ID": book_id}
    if control_url:
        payload["COUNTBEAN_CONTROL_URL"] = control_url
    # Create with 0600 from the start: writing then chmod'ing leaves the key
    # world-readable for the width of the write.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    return path


def forget() -> bool:
    """Delete the saved connection. True if there was one."""
    path = credentials_path()
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ------------------------------------------------------------- resolution ----
def resolve() -> dict[str, str]:
    """Merge the three sources, highest precedence last-write-wins."""
    merged: dict[str, str] = {}
    merged.update(_read_saved())
    merged.update(_read_env_file())
    for key in KNOWN_KEYS:
        value = os.environ.get(key)
        if value:
            merged[key] = value
    return merged


def source_of(key: str) -> str:
    """Which source supplied ``key`` — for telling the customer what they are
    connected to and why. A wrong-book connection is the expensive mistake
    here, and "which of my three configs won" is the question that diagnoses
    it."""
    if os.environ.get(key):
        return "environment"
    if key in _read_env_file():
        path = env_file_path()
        return f".env ({path})" if path else ".env"
    if key in _read_saved():
        return f"saved connection ({credentials_path()})"
    return "unset"


# ------------------------------------------------ pending device grant -------
# Held between `start_device_authorization` and `await_device_approval`.
#
# It exists so the two tools can be two tools. An MCP tool returns one result at
# the end, so a single tool that fetched the code and then waited could never
# show the code to the person who has to type it — it could only expire. Keeping
# the in-flight grant here means step 1 can return immediately and step 2 can
# pick it up, without the device code (a bearer credential) having to travel
# through the conversation.
#
# Same 0600 treatment as the saved connection: whoever holds the device code
# collects an API key for somebody's book.

def pending_device_path() -> Path:
    return credentials_path().parent / "pending-device.json"


def save_pending_device(grant: dict) -> Path:
    path = pending_device_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(grant, fh, indent=2)
        fh.write("\n")
    return path


def load_pending_device() -> dict | None:
    try:
        data = json.loads(pending_device_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("device_code") else None


def clear_pending_device() -> None:
    """Always called once a grant resolves, in either direction.

    A stale pending file would make the next `await_device_approval` poll a
    device code that is already claimed, and answer "not valid" to somebody who
    had just approved a fresh one.
    """
    try:
        pending_device_path().unlink()
    except OSError:
        pass
