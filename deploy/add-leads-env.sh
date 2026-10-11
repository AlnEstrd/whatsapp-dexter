#!/usr/bin/env bash
# Add the leads settings to the droplet's .env and restart the app. Run ON THE DROPLET:
#
#   sudo bash /opt/whatsapp-bot/deploy/add-leads-env.sh                  # asks for the Slack channel
#   sudo SLACK_LEADS_CHANNEL_ID=C0123 bash /opt/whatsapp-bot/deploy/add-leads-env.sh
#   sudo NO_RESTART=1 bash ...                                          # only edit .env
#
# - Backs up .env first (.env.bak-leads-<timestamp>, mode 600).
# - Idempotent: keys already present are left as they are (never overwritten), so it's safe
#   to run twice. To change a value later, edit .env by hand.
# - Generates DASHBOARD_PASSWORD (32 random chars) if it's missing and prints it ONCE:
#   save it in your password manager.
# - Zoho stays off (ZOHO_ENABLED=false); templates stay empty until Meta approves them.
set -euo pipefail

DEST="${DEST:-/opt/whatsapp-bot}"
ENV_FILE="$DEST/.env"
[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE not found" >&2; exit 1; }

backup="$ENV_FILE.bak-leads-$(date +%Y%m%d-%H%M%S)"
cp -p "$ENV_FILE" "$backup"
chmod 600 "$backup"
echo "Backup: $backup"

has_key() { grep -qE "^[[:space:]]*$1=" "$ENV_FILE"; }
added=()
add() {  # add KEY VALUE [comment] — only if KEY isn't already set
  local key="$1" value="$2" comment="${3:-}"
  if has_key "$key"; then
    echo "  = $key already set (kept)"
    return
  fi
  [[ -n "$comment" ]] && printf '# %s\n' "$comment" >> "$ENV_FILE"
  printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  added+=("$key")
  echo "  + $key"
}

# Slack channel for hot alerts (optional: empty = the existing SLACK_CHANNEL_ID)
channel="${SLACK_LEADS_CHANNEL_ID:-}"
if [[ -z "$channel" ]] && ! has_key SLACK_LEADS_CHANNEL_ID && [[ -t 0 ]]; then
  read -r -p "Slack channel ID for #dexter-leads (C0..., Enter to skip): " channel
fi
if [[ -n "$channel" && ! "$channel" =~ ^[CG][A-Z0-9]{6,}$ ]]; then
  echo "ERROR: '$channel' doesn't look like a Slack channel ID (C0123ABCD)" >&2
  exit 1
fi

# Dashboard password: generated here, never typed on the command line
password=""
if ! has_key DASHBOARD_PASSWORD; then
  password="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
fi

# Make sure the file ends with a newline before appending
[[ -s "$ENV_FILE" && -n "$(tail -c1 "$ENV_FILE")" ]] && echo >> "$ENV_FILE"
printf '\n# --- Leads (added %s by deploy/add-leads-env.sh) ---\n' "$(date +%F)" >> "$ENV_FILE"

add LEADS_ENABLED true "consent, scoring, Slack hot alerts, BAJA opt-out, contact form"
add CONTACT_FORM_ENABLED true
add LEAD_SLA_HOURS 24
add LEAD_RETENTION_DAYS 365
add METRICS_DIGEST_HOUR 8
[[ -n "$channel" ]] && add SLACK_LEADS_CHANNEL_ID "$channel" "hot alerts + digest (/invite the app there)"
add DASHBOARD_ENABLED true "https://origin.alanestrada.space/dashboard"
[[ -n "$password" ]] && add DASHBOARD_PASSWORD "$password"
add DASHBOARD_BASE_URL https://origin.alanestrada.space
add TEMPLATE_FOLLOWUP_ES "" "fill in after Meta approves the templates (deploy/leads/whatsapp-templates.md)"
add TEMPLATE_FOLLOWUP_EN ""
add ZOHO_ENABLED false "Zoho CRM sync: after the talk (deploy/leads/CHECKLIST.md step 5)"

chmod 600 "$ENV_FILE"
id deploy >/dev/null 2>&1 && chown deploy:deploy "$ENV_FILE" "$backup" || true

for key in LEADS_ENABLED DASHBOARD_ENABLED; do
  if grep -qiE "^[[:space:]]*$key=[[:space:]]*(false|0|no)?[[:space:]]*$" "$ENV_FILE"; then
    echo "WARNING: $key is already set to false/empty in .env and was kept. Edit .env to turn it on." >&2
  fi
done

if (( ${#added[@]} == 0 )); then
  echo "Nothing to add: every leads setting was already in .env."
fi

if [[ -n "$password" ]]; then
  echo
  echo "================ DASHBOARD PASSWORD (shown once, save it now) ================"
  echo "  $password"
  echo "=============================================================================="
fi

if [[ "${NO_RESTART:-}" == 1 ]]; then
  echo "NO_RESTART=1: .env updated, app not restarted."
  exit 0
fi

echo
echo "Restarting the app with the new settings..."
cd "$DEST/deploy"
docker compose up -d --no-build --force-recreate app

for _ in $(seq 1 30); do
  if docker compose exec -T app python -c '
import json, urllib.request
d = json.load(urllib.request.urlopen("http://127.0.0.1:8000/", timeout=3))
raise SystemExit(0 if d.get("status") == "ok" else 1)' >/dev/null 2>&1; then
    echo "App is healthy."
    code=$(docker compose exec -T app python -c '
import urllib.request, urllib.error
try:
    print(urllib.request.urlopen("http://127.0.0.1:8000/dashboard", timeout=3).status)
except urllib.error.HTTPError as e:
    print(e.code)' 2>/dev/null | tr -d '\r')
    echo "Dashboard: HTTP $code (200 = on; 404 = check DASHBOARD_ENABLED / password length)"
    echo "Open https://origin.alanestrada.space/dashboard on your phone."
    exit 0
  fi
  sleep 2
done
echo "WARNING: the app didn't report healthy in 60 s. Check: cd $DEST/deploy && docker compose logs --tail 50 app" >&2
echo "To undo: cp $backup $ENV_FILE && docker compose up -d --no-build --force-recreate app" >&2
exit 3
