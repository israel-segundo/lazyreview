# syntax=docker/dockerfile:1

FROM node:22-slim AS css
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY src/lazyreview/web ./src/lazyreview/web
RUN npm run build:css


FROM python:3.12-slim-bookworm

ARG UID=1000
ARG GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git jq ripgrep procps \
 && install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# Agent CLIs install into $HOME by default. Install each under /opt and link into
# /usr/local/bin so the /home/app volume can never shadow or pin a stale version.
# Versions are pinned because lazyreview parses each CLI's output; bump them
# deliberately (--build-arg) and rerun the tests. agy's installer only serves
# the latest release.
ARG CLAUDE_VERSION=2.1.300
ARG CURSOR_AGENT_VERSION=2026.10.14-941492e
ARG CODEX_VERSION=0.162.1
LABEL lazyreview.claude-version="${CLAUDE_VERSION}" \
      lazyreview.cursor-agent-version="${CURSOR_AGENT_VERSION}" \
      lazyreview.codex-version="${CODEX_VERSION}"
RUN HOME=/opt/claude bash -c 'curl -fsSL https://claude.ai/install.sh | bash -s "$0"' "${CLAUDE_VERSION}" \
 && ln -s "$(readlink -f /opt/claude/.local/bin/claude)" /usr/local/bin/claude \
 && chmod -R a+rX /opt/claude \
 && claude --version | grep -F "${CLAUDE_VERSION}"
RUN case "$(uname -m)" in x86_64) arch=x64 ;; aarch64) arch=arm64 ;; *) echo "unsupported arch" >&2; exit 1 ;; esac \
 && install -d /opt/cursor \
 && curl -fsSL "https://downloads.cursor.com/lab/${CURSOR_AGENT_VERSION}/linux/${arch}/agent-cli-package.tar.gz" \
      | tar --strip-components=1 -xzf - -C /opt/cursor \
 && ln -s /opt/cursor/cursor-agent /usr/local/bin/agent \
 && chmod -R a+rX /opt/cursor \
 && agent --version | grep -F "${CURSOR_AGENT_VERSION}"
RUN HOME=/opt/agy bash -c 'curl -fsSL https://antigravity.google/cli/install.sh | bash -s -- --dir /usr/local/bin' \
 && rm -rf /opt/agy \
 && agy --version
RUN HOME=/opt/codex sh -c 'curl -fsSL https://chatgpt.com/codex/install.sh | sh -s -- --release "$0"' "${CODEX_VERSION}" \
 && ln -s "$(readlink -f /opt/codex/.local/bin/codex)" /usr/local/bin/codex \
 && chmod -R a+rX /opt/codex \
 && codex --version | grep -F "${CODEX_VERSION}"

RUN groupadd -g "${GID}" app \
 && useradd -m -u "${UID}" -g "${GID}" -s /bin/bash app \
 && install -d -o app -g app /data \
      /home/app/.claude /home/app/.codex /home/app/.cursor \
      /home/app/.gemini /home/app/.gemini/antigravity-cli

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY --from=css /build/src/lazyreview/web/static/css/app.css ./src/lazyreview/web/static/css/app.css
RUN pip install --no-cache-dir . && rm -rf /app/src

COPY docker/entrypoint.sh /usr/local/bin/lazyreview-entrypoint
RUN chmod 0755 /usr/local/bin/lazyreview-entrypoint

ENV PYTHONUNBUFFERED=1 \
    LAZYREVIEW_HOST=0.0.0.0 \
    LAZYREVIEW_PORT=3334 \
    LAZYREVIEW_DATA_DIR=/data \
    LAZYREVIEW_CONFIG=/data/config.toml \
    DISABLE_AUTOUPDATER=1 \
    AGY_CLI_DISABLE_AUTO_UPDATE=true

USER app
WORKDIR /home/app
EXPOSE 3334

ENTRYPOINT ["lazyreview-entrypoint"]
CMD ["gunicorn", "lazyreview.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "1", \
     "--bind", "0.0.0.0:3334", \
     "--timeout", "120"]
