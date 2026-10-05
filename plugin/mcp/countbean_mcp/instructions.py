"""The portable "keep books the finkpr way" pack, for ANY assistant.

finkpr's own assistant (Hermes, in Telegram and WhatsApp) is told how to keep
books by its SOUL (book-runtime hermes/render_config.py). A customer's own
assistant — Claude, ChatGPT, Cursor, OpenClaw, Muse — reaches the same book
through this MCP server and was told nothing. This is the same rulebook, said
for them: propose before writing, never multiply rates, ask when unsure.

ONE TEXT, SERVED FOUR WAYS, so no copy can drift:

* ``finkpr-bookkeeping.md`` beside this file — the source, and the plain
  markdown a person can paste into an assistant's custom instructions;
* an MCP prompt and an MCP resource (server.py), for clients that offer them;
* the control plane's hosted endpoint, which carries the text in its
  generated manifest (``python -m countbean_mcp.manifest``) and serves it
  at ``/mcp/instructions.md``.

It is not the SOUL verbatim on purpose. The SOUL is written for a phone (no
markdown, three lines) and for an agent that sends reminders through a chat;
a desktop assistant has neither. The bookkeeping rules are the same rules.
"""
from __future__ import annotations

from pathlib import Path

_SOURCE = Path(__file__).with_name("finkpr-bookkeeping.md")

BOOKKEEPING: str = _SOURCE.read_text(encoding="utf-8")

PROMPT_NAME = "finkpr_bookkeeping"
RESOURCE_URI = "finkpr://instructions/bookkeeping"
PROMPT_DESCRIPTION = (
    "How to keep these books: propose before writing, never multiply by a "
    "rate yourself, ask when unsure. Read it before changing the book."
)

# Sent to every client at `initialize` (the MCP `instructions` field), so it is
# what an assistant sees even when it never opens the prompt or the resource.
# Short on purpose: some clients put it in every turn's context.
SERVER_INSTRUCTIONS = (
    "finkpr keeps one person's double-entry books (Beancount). Every write is "
    "checked with bean-check and committed to git. Before any tool that "
    "changes the book, show the person exactly what you will record and wait "
    "for a yes. Never convert currencies or multiply by a rate yourself: "
    "convert inside run_query with convert(), or write the rate exactly as "
    "the source gives it. When unsure which account, ask, or flag the entry "
    "with '!'. Never invent rows. Statements go through propose_transactions "
    "and commit_proposal, not retyped. The full rules are the "
    f"'{PROMPT_NAME}' prompt and the {RESOURCE_URI} resource."
)
