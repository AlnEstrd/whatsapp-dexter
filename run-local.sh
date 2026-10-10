#!/usr/bin/env bash
# Run the WhatsApp bot locally with one command (macOS).
#
#   ./run-local.sh              start: server + ngrok + shell tabs, open browser tabs
#   ./run-local.sh --consoles   also open Meta / Slack / Twilio dashboards
#   ./run-local.sh stop         stop uvicorn (port 8000) and ngrok
#
# Terminal tabs (iTerm2 if installed, otherwise Terminal.app):
#   1. uvicorn app.main:app --reload     2. ngrok http 8000     3. shell with .venv active
# Browser tabs: ngrok inspector (replay webhooks), GET / health, /docs.
#
# If PUBLIC_BASE_URL in .env is an ngrok domain, ngrok reuses it, so the Meta,
# Twilio and Slack webhook URLs don't change between runs.
set -euo pipefail

PORT="${PORT:-8000}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

say()  { printf '\033[1;32m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

stop_all() {
  local pids
  pids="$(lsof -ti tcp:"$PORT" 2>/dev/null || true)"
  [[ -n "$pids" ]] && kill $pids 2>/dev/null && say "Stopped server on :$PORT"
  pkill -f "ngrok http" 2>/dev/null && say "Stopped ngrok" || true
}

if [[ "${1:-}" == "stop" ]]; then stop_all; exit 0; fi
CONSOLES=false; [[ "${1:-}" == "--consoles" ]] && CONSOLES=true

# --- Preflight ---------------------------------------------------------------
[[ "$(uname)" == "Darwin" ]] || die "This script uses AppleScript to open tabs (macOS only)."
command -v ngrok >/dev/null || die "ngrok not found. Install: brew install ngrok && ngrok config add-authtoken <token>"
[[ -f .env ]] || die ".env missing. Run: cp .env.example .env  and fill it in."

if [[ ! -x .venv/bin/python ]]; then
  say "Creating .venv"
  python3 -m venv .venv
fi
# Reinstall deps only when requirements changed since the last install.
if [[ ! -f .venv/.deps-installed || requirements.txt -nt .venv/.deps-installed || requirements-dev.txt -nt .venv/.deps-installed ]]; then
  say "Installing dependencies"
  .venv/bin/pip install -q -r requirements-dev.txt && touch .venv/.deps-installed
fi

if lsof -ti tcp:"$PORT" >/dev/null 2>&1 || pgrep -f "ngrok http" >/dev/null; then
  warn "Server or ngrok already running; restarting them."
  stop_all; sleep 1
fi

# PUBLIC_BASE_URL is not a secret; read just that line from .env.
PUBLIC_BASE_URL="$(grep -E '^PUBLIC_BASE_URL=' .env | tail -1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//; s/^["'\'']//; s/["'\'']$//; s:/*$::')"
NGROK_DOMAIN=""
if [[ "$PUBLIC_BASE_URL" =~ ^https://([^/]+\.ngrok(-free)?\.(app|dev|io))$ ]]; then
  NGROK_DOMAIN="${BASH_REMATCH[1]}"
fi
if [[ -n "$NGROK_DOMAIN" ]]; then
  NGROK_CMD="ngrok http --domain=$NGROK_DOMAIN $PORT"
else
  NGROK_CMD="ngrok http $PORT"
fi

SERVER_CMD="cd '$DIR' && source .venv/bin/activate && uvicorn app.main:app --reload --port $PORT"
TUNNEL_CMD="cd '$DIR' && $NGROK_CMD"
SHELL_CMD="cd '$DIR' && source .venv/bin/activate && clear && echo 'Shell ready: pytest | ./run-local.sh stop'"

# --- Terminal tabs ---------------------------------------------------------------
if [[ -d "/Applications/iTerm.app" ]]; then
  say "Opening iTerm2 tabs"
  osascript <<OSA
tell application "iTerm"
  activate
  set w to (create window with default profile)
  tell current session of w to write text "$SERVER_CMD"
  tell w to set t2 to (create tab with default profile)
  tell current session of t2 to write text "$TUNNEL_CMD"
  tell w to set t3 to (create tab with default profile)
  tell current session of t3 to write text "$SHELL_CMD"
end tell
OSA
else
  say "Opening Terminal tabs"
  osascript -e "tell application \"Terminal\" to activate" \
            -e "tell application \"Terminal\" to do script \"$SERVER_CMD\""
  open_tab() {
    # New tabs need Accessibility permission for System Events; fall back to a window.
    if ! osascript -e 'tell application "System Events" to keystroke "t" using command down' \
                   -e "delay 0.4" \
                   -e "tell application \"Terminal\" to do script \"$1\" in front window" 2>/dev/null; then
      osascript -e "tell application \"Terminal\" to do script \"$1\""
    fi
  }
  sleep 0.5; open_tab "$TUNNEL_CMD"
  sleep 0.3; open_tab "$SHELL_CMD"
fi

# --- Wait for server + tunnel ------------------------------------------------------
say "Waiting for the server on :$PORT"
for _ in $(seq 1 40); do curl -fsS "http://localhost:$PORT/" >/dev/null 2>&1 && break; sleep 0.5; done
curl -fsS "http://localhost:$PORT/" >/dev/null 2>&1 || warn "Server not answering yet; check the uvicorn tab."

say "Waiting for ngrok"
TUNNEL_URL=""
for _ in $(seq 1 30); do
  TUNNEL_URL="$(curl -fsS http://localhost:4040/api/tunnels 2>/dev/null \
    | python3 -c 'import sys,json; t=[x["public_url"] for x in json.load(sys.stdin)["tunnels"] if x["public_url"].startswith("https")]; print(t[0] if t else "")' 2>/dev/null || true)"
  [[ -n "$TUNNEL_URL" ]] && break; sleep 0.5
done

# --- Browser tabs --------------------------------------------------------------------
open "http://localhost:4040"            # ngrok inspector: see & replay webhooks
open "http://localhost:$PORT/"          # health: channels, agent mode, slack_threads
open "http://localhost:$PORT/docs"      # FastAPI docs (admin endpoints)
if $CONSOLES; then
  open "https://developers.facebook.com/apps"   # Meta: WhatsApp > Configuration
  open "https://api.slack.com/apps"             # Slack app: Event Subscriptions
  open "https://console.twilio.com"             # Twilio sandbox / Flex
fi

# --- Summary -----------------------------------------------------------------------
echo
if [[ -z "$TUNNEL_URL" ]]; then
  warn "Couldn't read the ngrok URL; check the ngrok tab."
else
  say "Public URL: $TUNNEL_URL"
  if [[ "$TUNNEL_URL" != "$PUBLIC_BASE_URL" ]]; then
    warn "PUBLIC_BASE_URL in .env is '${PUBLIC_BASE_URL:-<empty>}'."
    warn "Set it to $TUNNEL_URL (Twilio signatures need it) and run ./run-local.sh again."
  fi
  cat <<TXT
  Meta webhook    $TUNNEL_URL/webhooks/meta
  Twilio webhook  $TUNNEL_URL/webhooks/twilio
  Slack events    $TUNNEL_URL/slack/events
TXT
fi
echo "Stop everything: ./run-local.sh stop"
