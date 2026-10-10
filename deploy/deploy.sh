#!/usr/bin/env bash
# Deploy from your Mac to the Droplet:   ./deploy/deploy.sh
# Needs the "whatsapp-bot" host in ~/.ssh/config.
set -euo pipefail
HOST="${HOST:-whatsapp-bot}"
DEST=/opt/whatsapp-bot
cd "$(dirname "$0")/.."

rsync -az --delete \
  --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/' \
  --exclude '_backup-*' --exclude '.env.bak*' --exclude 'Claude outputs/' --exclude '.DS_Store' --exclude 'data/' \
  ./ "$HOST:$DEST/"

ssh "$HOST" "chmod 600 $DEST/.env && cd $DEST/deploy && docker compose up -d --build && docker image prune -f >/dev/null && docker compose ps"
