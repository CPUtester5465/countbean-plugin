# Countbean plugin for Claude Code

Hosted plain-text accounting in Claude Code: a git-backed [Beancount](https://beancount.github.io/)
ledger, AI ingestion of statements and receipts into validated double-entry, and HTML/Excel reports.

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

Use the full HTTPS URL, not the `CPUtester5465/countbean-plugin` shorthand — the shorthand resolves
to `git@github.com`, which needs an SSH key *and* GitHub's host key already in your `known_hosts`.

Then connect it to your book. Say this to Claude with nothing after it:

```text
/countbean:connect
```

It prints a short code and a link. Open the link, sign in at
[app.countbean.com](https://app.countbean.com), pick the book, and approve —
Claude picks it up within a few seconds. There is no key to copy between
windows. (If you would rather paste a key you already have, put it on the same
line: `/countbean:connect cbk_… bok_…`.)

Full documentation is in [`plugin/README.md`](plugin/README.md).

---

## This repository is published, not authored

The source of truth is the `plugin/` directory of the Countbean monorepo. This repo is a mirror,
pushed by CI on every change.

**Pull requests here cannot be merged** — they would be overwritten by the next publish. The
mirror exists so that installing the plugin does not require access to the private monorepo,
and so `plugin/mcp/countbean_mcp/ledger.py` can keep being byte-compared against its
`ledger_core` original by tests that need both in one tree.
