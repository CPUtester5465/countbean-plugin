# Countbean plugin for Claude Code

Turn Claude Code into your bookkeeper. This plugin bundles:

- an **MCP server** that manages a git-backed [Beancount](https://beancount.github.io/) ledger (your "cloud book") — every write is validated with `bean-check` and committed to git, so the ledger is never left broken;
- **slash commands** to set it up, ingest data, and produce reports;
- a **skill** (`countbean-accounting`) that teaches Claude correct double-entry so transactions land valid the first time.

## Install

Run these one at a time — Claude Code reads a multi-line paste as a *single*
slash command, so sending them together makes `marketplace add` swallow the
install line and fail with `URL rejected: Malformed input to a URL function`.

```text
/plugin marketplace add https://github.com/CPUtester5465/countbean-plugin.git
```

```text
/plugin install countbean@countbean
```

Then reload, so the running session picks up the plugin's commands and MCP
tools — they are bound when Claude Code starts, so a fresh install is not live
until you do:

```text
/reload-plugins
```

Use the full HTTPS URL, not the `CPUtester5465/countbean` shorthand — the
shorthand resolves to `git@github.com`, which needs an SSH key *and* GitHub's
host key already in your `known_hosts`. On a machine without them the install
fails with `No ED25519 host key is known for github.com` before anything
Countbean-specific runs. This repo is public, so HTTPS needs no credentials at
all.

(Or locally from a clone: `/plugin marketplace add ./countbean` then the same install.)

On first use the MCP server builds a small Python virtualenv (beancount, beanquery, openpyxl, mcp) — this takes ~30s once. You'll be asked to approve the MCP server; run `/reload-plugins` again if the tools still don't appear.

**Requirements:** `python3` and `git` on your PATH.

If the server fails to connect (`Failed to reconnect to plugin:countbean:countbean: -32000`),
run `/mcp` — the launcher prints the real reason to stderr and Claude Code shows it there.
The usual cause is a `pip` that cannot install into a virtualenv; `run.sh` neutralises the
common culprits (`PIP_USER`, `PIP_TARGET`, `PIP_PREFIX`) and rebuilds an environment that
was left half-installed, so `/reload-plugins` is a real retry rather than a repeat of the
same failure.

## Commands

| Command | What it does |
|---|---|
| `/countbean:connect cbk_… bok_…` | Connect to a book hosted by Countbean. One line, takes effect immediately. |
| `/countbean:init [name] [currency]` | Connect to a hosted book, or create a local one, plus a starter chart of accounts. |
| `/countbean:ingest <file or text>` | Import a bank/card statement, CSV, OFX, PDF, or receipt — or plain text — into validated transactions. |
| `/countbean:report [html\|excel\|both] [as-of]` | Generate a styled HTML report and/or an Excel workbook (balance sheet, income statement, transactions). |
| `/countbean:status [question]` | Net worth, balances, recent changes — or answer a specific money question. |
| `/countbean:undo [commit]` | Revert a change from git history. |

## Where your data lives

By default the book is a git repo at `~/.countbean/main`. Point it elsewhere (e.g. a private GitHub
repo you push to) by setting `COUNTBEAN_BOOK` before Claude Code starts, or editing the `env` in the
plugin's `.mcp.json`. Because it's just plain text under git, you own it completely and can
`git push` it anywhere.

### Using a book hosted by Countbean

Open the book at [app.countbean.com](https://app.countbean.com) → **Connect Claude** → **Create
key**. It gives you one line to paste into Claude:

```
/countbean:connect cbk_… bok_…
```

Or let Claude do it without you copying anything: `/countbean:init` asks Countbean for a short
code, shows it to you, and you approve it in the browser — the key never passes through your
hands.

That is the whole setup. The pair is verified against the live book before anything is saved,
and it takes effect on your **next message** — the plugin resolves credentials on every tool
call, not at launch, so there is nothing to restart.

Credentials are read from three places, highest priority first:

| Source | Use it for |
|---|---|
| `COUNTBEAN_API_KEY` / `COUNTBEAN_BOOK_ID` in the environment | scripts, CI, a machine configured before Claude starts |
| a `.env` in the directory you run Claude from (searched up to 4 levels up) | per-project setup you want to see and commit around |
| `~/.countbean/credentials.json` (0600), written by `/countbean:connect` | everything else |

Only `COUNTBEAN_*` names are ever read out of a `.env`, and it is parsed as data — no
interpolation, no command substitution.

`connection_status` tells you which book you are connected to **and which of the three sources
chose it**, which is the question worth asking when a write lands somewhere unexpected.
`disconnect_book` forgets the saved connection (the key itself stays valid until you revoke it
on the book's page).

## MCP tools (for reference)

`start_device_authorization`, `await_device_approval`, `connect_book`, `disconnect_book`,
`connection_status`, `create_book`, `book_status`,
`list_accounts`, `get_ledger`, `open_accounts`, `add_transactions`, `add_directives`,
`propose_transactions`, `run_query`, `balances`, `assess_book`, `generate_report`,
`history`, `revert`, `stage_receipt`, `propose_receipt_transaction`.

`propose_transactions` is the statement importer: give it a CSV or OFX/QFX and the account it
belongs to, and it returns proposed transactions plus the column mapping it detected.

`stage_receipt` and `propose_receipt_transaction` are the receipt path. The first keeps the photo
or PDF as evidence and returns a reference; the second turns what you read off it into a proposed
entry with anything uncertain flagged rather than filled in. Neither does OCR: the reading is the
model's own, on purpose.

None of the three writes — `add_transactions` is still the only thing that does.

## Development

```
cd plugin/mcp
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
COUNTBEAN_BOOK=/tmp/testbook .venv/bin/python -m countbean_mcp   # runs the stdio server
```
