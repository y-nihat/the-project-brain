# The orchestrator ("brain") image. Trusted code authored by us; it controls
# the host Docker daemon only via the whitelisted socket-proxy (see compose).
#
# The Python Agent SDK drives the Claude Code CLI as a subprocess, so the image
# needs Node + the `claude` binary in addition to the Python deps.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Node, from NodeSource's SIGNED apt repository rather than `curl … | bash -`.
# The old form executed whatever that URL returned, as root, on every build:
# a compromise upstream was a compromise on every user's machine, with nothing
# to review and nothing to pin.
ARG NODE_MAJOR=22
# Pinned, because `npm install -g @anthropic-ai/claude-code` took whatever was
# newest at build time — two users building a day apart ran different CLIs and
# neither could say which.
ARG CLAUDE_CODE_VERSION=2.1.220

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Source tree is mounted over /app by docker-compose; this keeps the image
# usable standalone too.
COPY . /app

# Dependencies come from pyproject.toml — ONE declaration. The hand-written pip
# list that used to live here had already drifted from it, so the image and the
# distribution disagreed about what this project depends on.
RUN pip install --no-cache-dir -e ".[dev]"

CMD ["python", "-c", "import brain; print('brain image OK')"]
