#!/usr/bin/env bash
# Launcher for the Countbean MCP server.
#
# On first run it creates a self-contained virtualenv and installs deps
# (beancount, beanquery, openpyxl, mcp). Subsequent runs are instant.
#
# WHY THIS FILE IS SHAPED LIKE THIS
# ---------------------------------
# The original was four lines shorter and had two defects that compounded into
# the single worst first-run experience the product can produce: Claude Code
# reports `Failed to reconnect to plugin:countbean:countbean: -32000` and the
# customer has nothing else to go on.
#
#   1. The readiness check was `[ ! -x "$VENV/bin/python" ]` — "does the venv
#      DIRECTORY exist", not "does the server actually import". `python3 -m
#      venv` creates that interpreter as its FIRST act and `pip install` runs
#      after. So any failed install left a venv that exists and is empty, and
#      every subsequent launch skipped installation forever. The failure was
#      sticky: reinstalling the plugin does not clear it, because the venv is
#      in the plugin cache and the cache is what gets reused.
#
#   2. `pip install -q ... >/dev/null` under `set -e` sent the reason to
#      /dev/null and the exit status to a shell that died silently on stdio.
#      MCP saw the pipe close and reported -32000. The actual message — the one
#      sentence that names the cause — was discarded on the way.
#
# MEASURED, on the reporter's own laptop, 2026-08-14: `PIP_USER=1` was exported
# in their shell (the common workaround for PEP 668 "externally-managed-
# environment"). pip inherits it inside the venv and refuses every install with
# "Can not perform a '--user' install. User site-packages are not visible in
# this virtualenv." The venv was real, `pip list` was empty, and `import mcp`
# raised ModuleNotFoundError on every launch.
#
# So: neutralise the inherited pip config, verify by IMPORT rather than by
# directory, and let failures speak. stderr from an MCP server is captured by
# the client and shown with `/mcp` — it is the only channel a stdio server has
# for saying why it did not start.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
PYTHON="${COUNTBEAN_PYTHON:-python3}"
# Written only after the imports below succeed, so a half-finished install is
# never mistaken for a finished one.
STAMP="$VENV/.deps-ok"

# The customer's pip settings must not reach this venv. `PIP_USER` is the one
# that has actually bitten; the rest redirect an install somewhere other than
# the venv we are about to run from, with the same end state (a venv that
# imports nothing) and the same unhelpful -32000.
#
# `PIP_USER=0` rather than `unset`: unsetting the variable still leaves a
# `user = true` in ~/.config/pip/pip.conf in force, and pip resolves env above
# config. Verified against both a hostile env var and a hostile pip.conf.
export PIP_USER=0
unset PIP_TARGET PIP_PREFIX PIP_ROOT PIP_REQUIRE_VIRTUALENV 2>/dev/null || true

# Everything the server needs to import. Checked as one unit: a partial install
# (network dropped mid-download) fails exactly like no install at all.
IMPORT_CHECK='import mcp.server.fastmcp, beancount, beanquery, openpyxl'

deps_ready() {
  [ -x "$VENV/bin/python" ] && [ -f "$STAMP" ] &&
    "$VENV/bin/python" -c "$IMPORT_CHECK" >/dev/null 2>&1
}

install_deps() {
  local log
  log="$(mktemp -t countbean-install)"

  if [ ! -x "$VENV/bin/python" ]; then
    echo "countbean: creating Python environment (first run, ~30s)…" >&2
    if ! "$PYTHON" -m venv "$VENV" >"$log" 2>&1; then
      echo "countbean: could not create a virtualenv with '$PYTHON'." >&2
      sed 's/^/  /' "$log" >&2
      echo "countbean: set COUNTBEAN_PYTHON to a Python 3.11+ interpreter and retry." >&2
      rm -f "$log"
      return 1
    fi
  fi

  echo "countbean: installing dependencies…" >&2
  "$VENV/bin/pip" install --upgrade pip >"$log" 2>&1 || true  # cosmetic; never fatal
  if ! "$VENV/bin/pip" install -r "$HERE/requirements.txt" >"$log" 2>&1; then
    echo "countbean: dependency install FAILED. pip said:" >&2
    tail -n 25 "$log" | sed 's/^/  /' >&2
    rm -f "$log"
    return 1
  fi
  rm -f "$log"

  # Prove it before claiming it. This is the check that the old `-x` test was
  # standing in for, and the reason a failed install can no longer stick.
  if ! "$VENV/bin/python" -c "$IMPORT_CHECK" >/dev/null 2>&1; then
    echo "countbean: dependencies installed but do not import. Details:" >&2
    "$VENV/bin/python" -c "$IMPORT_CHECK" 2>&1 | sed 's/^/  /' >&2
    return 1
  fi
  : >"$STAMP"
  echo "countbean: ready." >&2
}

if ! deps_ready; then
  # A venv that exists but does not import is the sticky state described above.
  # Rebuilding from scratch is the only reliable way out, and it costs one slow
  # start on a machine that was already broken.
  if [ -d "$VENV" ] && [ ! -f "$STAMP" ] && [ -x "$VENV/bin/python" ]; then
    if ! "$VENV/bin/python" -c "$IMPORT_CHECK" >/dev/null 2>&1; then
      echo "countbean: previous install was incomplete — rebuilding it." >&2
      rm -rf "$VENV"
    fi
  fi
  install_deps || {
    echo "countbean: the MCP server cannot start until the above is fixed." >&2
    exit 1
  }
fi

# Put the venv's console scripts (bean-check, bean-query) and the package
# on the path the server process will use.
export PATH="$VENV/bin:$PATH"
export PYTHONPATH="$HERE:${PYTHONPATH:-}"

exec "$VENV/bin/python" -m countbean_mcp
