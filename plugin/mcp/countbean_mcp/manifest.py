"""Print the hosted endpoint's manifest: what the control plane answers WITHOUT a book.

    python -m countbean_mcp.manifest > platform/apps/control-plane/src/mcp/manifest.json

The control plane answers `initialize`, `tools/list`, `prompts/*` and
`resources/*` itself, so a client connecting — Claude Desktop does it at every
launch — does not wake a book that scales to zero. It can only do that from a
copy of the definitions, and a hand-kept copy is the drift this design exists
to avoid. So the copy is GENERATED from the running remote server, committed,
and ``test_hosted_mcp.py`` regenerates it and fails when the committed file is
not byte-identical. Change a tool, run the command above.
"""
from __future__ import annotations

import inspect
import json
import sys

import anyio

from . import instructions, remote


async def _collect() -> dict:
    srv = remote.build_server()
    tools = await srv.list_tools()
    prompts = await srv.list_prompts()
    resources = await srv.list_resources()

    def dump(obj) -> dict:
        return obj.model_dump(by_alias=True, exclude_none=True, mode="json")

    return {
        "_generated_by": "python -m countbean_mcp.manifest — do not edit by hand",
        "server": {
            "name": remote.SERVER_NAME,
            "version": remote.server_version(),
            "instructions": instructions.SERVER_INSTRUCTIONS,
        },
        "tools": [_clean(dump(t)) for t in tools],
        "prompts": [
            {"definition": dump(p), "text": instructions.BOOKKEEPING} for p in prompts
        ],
        "resources": [
            {"definition": dump(r), "text": instructions.BOOKKEEPING} for r in resources
        ],
    }


def _clean(tool: dict) -> dict:
    """Dedent the description the way Python 3.13+ already does.

    A tool's description is its docstring, and 3.13 started dedenting
    docstrings at compile time — so the raw text depends on which Python
    generated it, and the committed file flapped between a laptop (3.14) and
    CI (3.12). `inspect.cleandoc` gives every Python the same text, which is
    also the text a client should be shown. Whitespace only: no word changes.
    """
    if isinstance(tool.get("description"), str):
        tool["description"] = inspect.cleandoc(tool["description"])
    return tool


def render() -> str:
    return json.dumps(anyio.run(_collect), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def main() -> None:
    sys.stdout.write(render())


if __name__ == "__main__":
    main()
