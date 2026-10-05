"""Which book a tool call is for, when the call came over HTTP.

The stdio server (a laptop, or the hosted agent inside a book) resolves its
book from the process: environment, a `.env`, the saved connection. That is
right for a process that serves ONE person. The hosted MCP endpoint
(``remote.py``) is a different shape: one process on the book machine, and
every request carries its own credential. So the book must come from the
REQUEST, and never from anything the process happens to have lying around.

``server._book()`` asks this module first. The rule it enforces:

* no MCP request in flight, or one with no HTTP request behind it (stdio)
  → ``None``, and the old resolution runs unchanged;
* an HTTP request that ``remote.py``'s gate verified → that request's config;
* an HTTP request the gate did NOT verify → **refuse**. Falling back to the
  process's credentials there is the one mistake that could hand a caller a
  book that is not theirs, so it is not a fallback that exists.
"""
from __future__ import annotations

from typing import Optional

from .hosted import HostedConfig, HostedConfigError

# Keys on the ASGI scope, set by remote.py's gate after it verified the
# request. A scope key rather than a contextvar on purpose: the MCP session
# manager runs the tool in ITS task group, whose context was copied when the
# app started — a contextvar set by the gate would never reach the tool. The
# Starlette request the transport hands the tool wraps this same scope dict.
CONFIG_KEY = "countbean.hosted_config"
APP_URL_KEY = "countbean.app_url"


def _http_request():
    """The Starlette request behind the current MCP call, or None (stdio)."""
    try:
        from mcp.server.lowlevel.server import request_ctx
    except ImportError:  # pragma: no cover — an mcp without the lowlevel server
        return None
    try:
        ctx = request_ctx.get()
    except LookupError:
        return None
    return getattr(ctx, "request", None)


def is_remote() -> bool:
    """Is the current tool call one the hosted HTTP endpoint is serving?"""
    return _http_request() is not None


def hosted_request_config() -> Optional[HostedConfig]:
    """The book THIS request may reach, or None outside an HTTP request."""
    request = _http_request()
    if request is None:
        return None
    scope = getattr(request, "scope", None)
    cfg = scope.get(CONFIG_KEY) if isinstance(scope, dict) else None
    if not isinstance(cfg, HostedConfig):
        raise HostedConfigError(
            "This request reached the hosted finkpr MCP server without a "
            "verified book credential, so it was refused. Nothing was read "
            "or written."
        )
    return cfg


def app_url() -> str:
    """The web app's origin the control plane named for this request, or ''."""
    request = _http_request()
    scope = getattr(request, "scope", None) if request is not None else None
    value = scope.get(APP_URL_KEY, "") if isinstance(scope, dict) else ""
    return value if isinstance(value, str) else ""
