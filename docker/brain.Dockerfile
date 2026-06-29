# The orchestrator ("brain") image. Trusted code authored by us; it controls
# the host Docker daemon only via the whitelisted socket-proxy (see compose).
#
# The Python Agent SDK drives the Claude Code CLI as a subprocess, so the image
# needs Node + the `claude` binary in addition to the Python deps.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies (source is bind-mounted at runtime; we install deps only).
RUN pip install --no-cache-dir \
        claude-agent-sdk \
        "docker>=7.0.0" \
        python-dotenv \
        pytest \
        pytest-asyncio

WORKDIR /app

# Source tree is mounted over /app by docker-compose; this keeps the image
# usable standalone too.
COPY . /app

CMD ["python", "-c", "import brain; print('brain image OK')"]
