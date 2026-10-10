#!/usr/bin/env bash
# Build, start and health-check the bot ON THE SERVER, rolling back if it's unhealthy.
# Used by CI (.github/workflows/deploy.yml) and by ./deploy/deploy.sh, after the code was rsynced:
#   bash /opt/whatsapp-bot/deploy/remote-deploy.sh <tag>      # tag = git sha
#
# Images: whatsapp-bot:<tag> (each build), :current (what compose runs), :previous (rollback target).
# Exit codes: 0 deployed | 1 precondition | 2 build failed (nothing changed)
#             3 unhealthy, rolled back to :previous | 4 unhealthy and rollback failed/impossible
set -euo pipefail

TAG="${1:-}"
if [[ ! "$TAG" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "usage: $0 <tag>   (git sha; letters, digits, . _ -)" >&2
  exit 1
fi

SELF="$(readlink -f "$0")"
DEST="$(cd "$(dirname "$SELF")/.." && pwd)"
IMAGE=whatsapp-bot
KEEP_BUILDS="${KEEP_BUILDS:-3}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"

# Manual deploys log in as root. Hand files to 'deploy' (if that user exists) and run as it,
# so CI and manual deploys never leave files the other one can't overwrite.
if [[ $EUID -eq 0 ]] && id deploy >/dev/null 2>&1; then
  chown -R deploy:deploy "$DEST"
  exec runuser -u deploy -- bash "$SELF" "$TAG"
fi

[[ -f "$DEST/.env" ]] || { echo "ERROR: $DEST/.env is missing (create it on the server or use ./deploy/deploy.sh --with-env)" >&2; exit 1; }
chmod 600 "$DEST/.env"

# One deploy at a time (CI concurrency only covers CI; this also covers manual deploys).
exec 9>"$DEST/.deploy.lock"
flock -w 900 9 || { echo "ERROR: another deploy is still running" >&2; exit 1; }

cd "$DEST/deploy"   # compose project dir: keeps the project name (and its volumes) unchanged
log() { echo "[deploy $(date -u +%H:%M:%S)] $*"; }
has_image() { docker image inspect "$1" >/dev/null 2>&1; }

healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  while (( SECONDS < deadline )); do
    if docker compose exec -T app python -c '
import json, sys, urllib.request
d = json.load(urllib.request.urlopen("http://127.0.0.1:8000/", timeout=3))
sys.exit(0 if d.get("status") == "ok" else 1)' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

start_current() {
  IMAGE_TAG=current docker compose up -d --no-build --force-recreate app
  IMAGE_TAG=current docker compose up -d --no-build   # caddy etc., no-op if already up
}

# :previous = what's running right now (falls back to the last :current).
# The running container is the truth: after a manual rollback, :current may be a bad build.
running="$(docker compose ps -q app 2>/dev/null | head -n1 || true)"
if [[ -n "$running" ]]; then
  docker tag "$(docker inspect -f '{{.Image}}' "$running")" "$IMAGE:previous"
elif has_image "$IMAGE:current"; then
  docker tag "$IMAGE:current" "$IMAGE:previous"
fi

log "building $IMAGE:$TAG"
if ! IMAGE_TAG="$TAG" docker compose build app; then
  log "BUILD FAILED, nothing was changed"
  exit 2
fi

docker tag "$IMAGE:$TAG" "$IMAGE:current"
log "starting $IMAGE:$TAG"
start_current

log "health check (up to ${HEALTH_TIMEOUT}s)"
if ! healthy; then
  log "UNHEALTHY: $TAG"
  docker compose ps app || true
  [[ "${SHOW_LOGS:-0}" == 1 ]] && docker compose logs --tail 40 app || true
  if ! has_image "$IMAGE:previous"; then
    log "no :previous image to roll back to"
    exit 4
  fi
  log "rolling back to :previous"
  docker tag "$IMAGE:previous" "$IMAGE:current"
  start_current
  if healthy; then
    log "ROLLED BACK; still serving $(cat "$DEST/DEPLOYED_SHA" 2>/dev/null || echo 'the previous build')"
    exit 3
  fi
  log "ROLLBACK ALSO UNHEALTHY, check the server: cd $DEST/deploy && docker compose logs app"
  exit 4
fi

echo "$TAG" > "$DEST/DEPLOYED_SHA.tmp" && mv "$DEST/DEPLOYED_SHA.tmp" "$DEST/DEPLOYED_SHA"
log "healthy, deployed $TAG"

# Disk is 25 GB: keep the last $KEEP_BUILDS build tags (+ :current/:previous), drop the rest.
docker image ls "$IMAGE" --format '{{.CreatedAt}}\t{{.Tag}}' \
  | { grep -vP '\t(current|previous|<none>)$' || true; } | sort -r | tail -n +"$((KEEP_BUILDS + 1))" | cut -f2 \
  | while read -r old; do docker image rm "$IMAGE:$old" >/dev/null 2>&1 || true; done
docker image prune -f >/dev/null
docker builder prune -f --filter until=168h >/dev/null 2>&1 || true
docker compose ps
