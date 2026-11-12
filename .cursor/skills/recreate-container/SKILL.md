---
name: recreate-container
description: >-
  Rebuild and recreate the lazyreview Docker Compose service with
  `docker compose up -d --build`, keeping named volumes. Use when the user asks
  to recreate, regenerate, rebuild, or restart the container, or says regenera
  container / recreate container / rebuild docker.
---

# Recreate container

Rebuild the lazyreview image and recreate the running Compose service. Data in
named volumes (`lazyreview-data`, `lazyreview-home`) is kept.

## Guardrails

- Run from the repo root (directory that contains `compose.yaml`).
- Prefer `docker compose up -d --build` over separate build + up.
- Do **not** run `docker compose down -v` unless the user explicitly asks to wipe
  volumes.
- Do not edit `.env`, credentials, or Compose files as part of this skill.
- Report success/failure briefly: build outcome, container status, health if available.

## Steps

1. Confirm Compose is available and you are in the repo root:

```bash
command -v docker
test -f compose.yaml
```

2. Rebuild and recreate (detached):

```bash
docker compose up -d --build
```

3. Verify the service:

```bash
docker compose ps
docker compose logs --tail=40 lazyreview
```

Optional health probe (UI listens on host port from `.env`, default `3334`):

```bash
curl -fsS -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:${LAZYREVIEW_HOST_PORT:-3334}/api/health" || true
```

4. Tell the user the container was recreated (or what failed). Mention
   http://127.0.0.1:3334 when the health check succeeds.

## Variants (only when the user asks)

| Ask | Command |
| --- | --- |
| Build only, do not start | `docker compose build` |
| Restart without rebuild | `docker compose restart` |
| Stop without deleting volumes | `docker compose down` |
| Wipe volumes (destructive) | confirm first, then `docker compose down -v` and `docker compose up -d --build` |
| Bump a pinned CLI build arg | `docker compose build --build-arg CODEX_VERSION=...` (or Claude/Cursor), then `docker compose up -d` |
