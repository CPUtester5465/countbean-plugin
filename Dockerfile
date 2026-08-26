# Container image for the Countbean MCP server.
#
# Exists so directories that verify a server by RUNNING it (Glama, and the
# awesome-mcp-servers listing gated on Glama's check) have something to build.
# It is NOT how a Claude Code user runs the plugin — that path is
# plugin/mcp/run.sh, which builds a virtualenv beside the plugin install.
#
# The server speaks MCP over stdio, so there is no port and no HEALTHCHECK:
# the check is that it starts and answers an introspection request on stdin.
#
# `bean-check` and `bean-query` are console scripts from the beancount and
# beanquery wheels, so installing the package puts them on PATH. ledger.py
# shells out to bean-check BY NAME; without it the server starts and every
# write fails, which is a worse failure than not starting at all.
FROM python:3.12-slim

WORKDIR /app

# Install from the package metadata rather than requirements.txt, so the image
# and `pip install countbean-mcp` resolve the same dependency set.
COPY pyproject.toml README.md ./
COPY plugin/mcp/countbean_mcp ./plugin/mcp/countbean_mcp
RUN pip install --no-cache-dir .

# stdio transport: unbuffered, or a response sits in a pipe buffer and the
# client times out waiting for a server that already answered.
ENV PYTHONUNBUFFERED=1

CMD ["countbean-mcp"]
