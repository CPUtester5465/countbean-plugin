"""The hosted MCP endpoint, book-machine half: Streamable HTTP over the SAME tools.

WHAT THIS IS
------------
``https://api.finkpr.com/mcp`` lets ANY assistant — Claude, ChatGPT, Cursor,
OpenClaw, a customer's own Hermes — reach a finkpr book with a book key
(``Authorization: Bearer cbk_…``). The control plane terminates that URL
(routes/mcp.ts): it checks the key, answers the protocol chatter
(`initialize`, `tools/list`, prompts, resources) from a generated manifest
without waking anything, and forwards each `tools/call` to THIS server on the
book's own machine, through the same wake-and-hold path as the ledger API.

WHY THE TOOLS RUN HERE, AND NOT AS A SECOND IMPLEMENTATION
----------------------------------------------------------
The 22 tools are Python in ``server.py``, and several carry real logic —
statement parsing and proposals, the assessment, the report wording. A
TypeScript copy in the control plane would be a second implementation of all
of it, and this repo's history is a list of the ways two copies drift. So the
hosted endpoint does not describe or run its own tools: it registers the very
functions ``server.py`` registers (same callable, same docstring, same
schema, same title and hints) and ``test_hosted_mcp.py`` asserts the listed
definitions are identical, tool for tool.

WHY NOT EVERY TOOL
------------------
``REMOTE_TOOLS`` is an ALLOWLIST, like Hermes' ``HOSTED_TOOLS`` and for the
same reason: a tool added to the plugin tomorrow is not reachable from the
internet until someone puts it here and says why. What is left out, and why,
is ``NOT_REMOTE`` — and the parity test fails if a stdio tool is in neither.

HOW A CALL FINDS ITS BOOK
-------------------------
The gate below verifies the request's credential (the sidecar hands it a
verifier: the book-scoped JWT the control plane minted for THIS call), builds
a ``HostedConfig`` for this book only, and puts it on the request scope.
``server._book()`` reads it from there (request_scope.py) and refuses outright
when it is missing. The tool then calls this machine's own sidecar with the
same JWT, so the sidecar enforces read versus readwrite exactly as it does for
every other caller. Nothing in this process can address another book: the JWT
names this book, the sidecar serves this book, and the machine holds no other.

WHY THE TOOLS RUN IN A THREAD
-----------------------------
FastMCP runs a synchronous tool ON the event loop. Here that is a deadlock,
not a slowdown: the tool calls the sidecar over HTTP, and the sidecar is this
same uvicorn process. So every tool is wrapped to run in a worker thread
(anyio copies the context, so the request is still visible to the tool).
"""
from __future__ import annotations

import functools
import json
from typing import Callable

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__, instructions, request_scope, server
from .hosted import HostedConfig

SERVER_NAME = "finkpr"

# What a customer's own assistant may call over the hosted endpoint. Grouped as
# Hermes' list is, read / propose / write.
REMOTE_TOOLS: tuple[str, ...] = (
    # read
    "book_status",
    "list_accounts",
    "balances",
    "get_ledger",
    "run_query",
    "history",
    "assess_book",
    "generate_report",
    # propose — stores the statement on the book, writes nothing to the ledger
    "propose_transactions",
    # write — bean-check and a git commit on the book, each
    "commit_proposal",
    "add_transactions",
    "open_accounts",
    "add_directives",
    "revert",
)

# Every stdio tool that is NOT reachable over HTTP, with the reason. The parity
# test requires stdio == REMOTE_TOOLS ∪ NOT_REMOTE exactly.
NOT_REMOTE: dict[str, str] = {
    "connect_book": "manages a laptop's saved connection file; the key in the request already chose the book",
    "disconnect_book": "same — there is no saved connection on this side",
    "start_device_authorization": "the laptop sign-in flow; an HTTP caller is already signed in by its key",
    "await_device_approval": "same",
    "connection_status": "reports the laptop's config sources and echoes key characters",
    "create_book": "refuses on every hosted book by design (hosted.py `init`)",
    "stage_receipt": "receipt bytes go to a bucket the control plane holds; phase 2",
    "propose_receipt_transaction": "needs a stored receipt from stage_receipt; phase 2",
}

# Response header the control plane reads to stamp the book's write columns
# (routes/mcp.ts). "write" = a customer write (activates the book), "revert" =
# an undo (stamps last_write_at only). Absent = nothing was written.
WROTE_HEADER = b"x-finkpr-book-wrote"
# Request header the control plane sets: the web app's origin, for links.
APP_URL_HEADER = "x-finkpr-app-url"


def _threaded(fn: Callable) -> Callable:
    """Run a synchronous tool in a worker thread (see the module docstring)."""

    @functools.wraps(fn)
    async def run(**kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))

    return run


def build_server() -> FastMCP:
    """A stateless, JSON-only FastMCP carrying the REMOTE_TOOLS of server.py."""
    remote = FastMCP(
        SERVER_NAME,
        instructions=instructions.SERVER_INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        # The default for a loopback host is DNS-rebinding protection with a
        # localhost allowlist, which refuses the Host the request really
        # arrives with. This server is not a browser-reachable localhost
        # service: it answers only behind the sidecar's JWT check, which is a
        # stronger property than a Host header.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    # The registered Tool objects, for their title, hints and description.
    # `_tool_manager` is FastMCP's own registry; the public `list_tools` is
    # async and this runs inside the sidecar's event loop at startup. The
    # parity test compares the PUBLIC listings of both servers, so a change in
    # this private shape fails a test rather than drifting.
    registry = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    for name in REMOTE_TOOLS:
        tool = registry[name]
        remote.add_tool(
            _threaded(getattr(server, name)),
            name=name,
            title=tool.title,
            description=tool.description,
            annotations=tool.annotations,
        )
    remote.prompt(
        name=instructions.PROMPT_NAME,
        title="Keep these books the finkpr way",
        description=instructions.PROMPT_DESCRIPTION,
    )(server.finkpr_bookkeeping)
    remote.resource(
        instructions.RESOURCE_URI,
        name="finkpr-bookkeeping",
        title="finkpr bookkeeping rules",
        description=instructions.PROMPT_DESCRIPTION,
        mime_type="text/markdown",
    )(server.finkpr_bookkeeping_resource)
    return remote


class Refused(Exception):
    """The gate's verifier refused the request. Carries the HTTP answer."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


Verifier = Callable[[object], HostedConfig]


class HostedGate:
    """ASGI app: verify, attach the request's book, run the MCP transport.

    ``verify(request)`` returns the ``HostedConfig`` this request may use, or
    raises ``Refused``. It is supplied by the host process (the sidecar), which
    is the one that knows how its callers authenticate.

    ⚠️ The session manager's lifespan is the HOST's to run:
    ``async with gate.session_manager.run(): ...`` in the sidecar's lifespan.
    Mounted as a plain route, the Starlette app's own lifespan never runs.
    """

    def __init__(self, verify: Verifier, mcp_server: FastMCP | None = None, path: str = "/mcp"):
        self.server = mcp_server or build_server()
        self.server.settings.streamable_http_path = path
        self.app = self.server.streamable_http_app()
        self.verify = verify

    @property
    def session_manager(self):
        return self.server.session_manager

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":  # pragma: no cover — lifespan is the host's
            return await self.app(scope, receive, send)

        from starlette.requests import Request

        request = Request(scope, receive)
        try:
            cfg = self.verify(request)
        except Refused as exc:
            return await _send_json(send, exc.status, {"error": {"code": exc.code, "message": exc.message}})

        writes: list[str] = []
        cfg.on_write = writes.append
        scope[request_scope.CONFIG_KEY] = cfg
        app_url = request.headers.get(APP_URL_HEADER, "")
        if app_url.startswith("https://") or app_url.startswith("http://"):
            scope[request_scope.APP_URL_KEY] = app_url

        async def send_with_writes(message):
            # JSON-response mode answers only after the tool returned, so every
            # accepted write is already in `writes` when the head goes out.
            if message.get("type") == "http.response.start" and writes:
                kind = b"write" if any(p != "/revert" for p in writes) else b"revert"
                message = dict(message)
                message["headers"] = list(message.get("headers") or []) + [(WROTE_HEADER, kind)]
            await send(message)

        await self.app(scope, receive, send_with_writes)


async def _send_json(send, status: int, body: dict) -> None:
    raw = json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())],
    })
    await send({"type": "http.response.body", "body": raw})


def server_version() -> str:
    return __version__
