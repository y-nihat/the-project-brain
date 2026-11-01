# The orchestrator ("brain") image. Trusted code authored by us; it controls
# the host Docker daemon only via the whitelisted socket-proxy (see compose).
#
# The Python Agent SDK drives the Claude Code CLI as a subprocess, so the image
# needs Node + the `claude` binary in addition to the Python deps.
FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Node, as the official release tarball VERIFIED AGAINST ITS PUBLISHED CHECKSUM.
#
# What this replaces: `curl -fsSL https://deb.nodesource.com/setup_22.x | bash -`,
# which executed whatever that URL returned, as root, on every build — nothing
# to review, nothing to pin, and a compromise upstream was a compromise on every
# user's machine. The obvious alternative, NodeSource's signed apt repo, was
# tried and rejected: its gpgkey endpoint answers 403, so the build depended on
# a URL that is not reliably reachable. Debian's own nodejs is 20.x, and the
# Claude Code CLI declares `engines: node >= 22`.
#
# So: one exact version, one exact hash, no third-party repository. Bump both
# lines together (checksum from https://nodejs.org/dist/<version>/SHASUMS256.txt).
ARG NODE_VERSION=v22.23.1
ARG NODE_SHA256_amd64=9749e988f437343b7fa832c69ded82a312e41a03116d766797ac14f6f9eee578
ARG NODE_SHA256_arm64=0294e8b915ab75f92c7513d2fcb830ae06e10684e6c603e99a87dbf8835389c1
# Pinned, because `npm install -g @anthropic-ai/claude-code` took whatever was
# newest at build time — two users building a day apart ran different CLIs and
# neither could say which.
ARG CLAUDE_CODE_VERSION=2.1.220
# Set by BuildKit. The base image is multi-arch, so hardcoding the x64 tarball
# made the build fail on Apple Silicon and on arm64 servers — for a project whose
# only supported path is `docker compose up`, that is "does not run here".
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl xz-utils \
    && NODE_ARCH="$(case "${TARGETARCH:-amd64}" in \
           amd64) echo x64 ;; arm64) echo arm64 ;; \
           *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; esac)" \
    && NODE_SHA="$(case "${TARGETARCH:-amd64}" in \
           amd64) echo "${NODE_SHA256_amd64}" ;; arm64) echo "${NODE_SHA256_arm64}" ;; esac)" \
    && curl -fsSL -o /tmp/node.tar.xz \
       "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" \
    && echo "${NODE_SHA}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
         --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md \
    && rm -f /tmp/node.tar.xz \
    && node --version && npm --version \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && apt-get purge -y curl xz-utils && apt-get autoremove -y \
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
