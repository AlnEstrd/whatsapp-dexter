#!/usr/bin/env bash
# Manual deploy from your Mac to the Droplet (CI does the same on every merge to main):
#   ./deploy/deploy.sh               # sync code, build, health-check, roll back if unhealthy
#   ./deploy/deploy.sh --with-env    # also push your local .env (only when you changed it)
#   ./deploy/deploy.sh --dry-run     # show what rsync would send/delete, change nothing
# Needs the "whatsapp-bot" host in ~/.ssh/config (override with HOST=...).
set -euo pipefail
HOST="${HOST:-whatsapp-bot}"
DEST=/opt/whatsapp-bot
cd "$(dirname "$0")/.."

WITH_ENV=0 DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --with-env) WITH_ENV=1 ;;
    --dry-run)  DRY_RUN=1 ;;
    -h|--help)  sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 1 ;;
  esac
done

# Image tag = commit; uncommitted changes get a -dirty-<time> suffix so they never reuse a clean tag.
TAG="$(git rev-parse --short=12 HEAD)"
[[ -z "$(git status --porcelain)" ]] || TAG="$TAG-dirty-$(date +%Y%m%d%H%M%S)"

RSYNC=(rsync -az --delete --exclude-from=deploy/rsync-excludes)
if (( DRY_RUN )); then
  "${RSYNC[@]}" --dry-run --itemize-changes ./ "$HOST:$DEST/"
  exit 0
fi

"${RSYNC[@]}" ./ "$HOST:$DEST/"
if (( WITH_ENV )); then
  [[ -f .env ]] || { echo "no local .env to send" >&2; exit 1; }
  rsync -z .env "$HOST:$DEST/.env"
  echo "pushed .env"
fi

# Same server-side script CI uses: build whatsapp-bot:$TAG, health check, roll back on failure.
ssh "$HOST" "chmod 600 $DEST/.env 2>/dev/null; bash $DEST/deploy/remote-deploy.sh '$TAG' '' manual"
