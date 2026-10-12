# WhatsApp Agentic Bot — POC

A provider-agnostic WhatsApp bot: **Twilio** (today) and **Meta Cloud API**
(after approval) behind one channel interface, a Claude tool-use agent in the
middle, and **Slack** for human escalation + Meta account alerts.

```
             ┌─ /webhooks/twilio ─ TwilioChannel ─┐
WhatsApp ──> │                                    ├─> handle_inbound ─> Agent (Claude | echo)
             └─ /webhooks/meta ─── MetaChannel ───┘          │
                                                             ├─ failure / "humano" ─> Slack + handoff
                 Meta account_review_update etc. ────────────┴─> Slack alert
```

- Replies always go back on the channel the message came in on, so both
  providers can run side by side during a migration.
- `WA_PROVIDER` only picks the channel for bot-initiated messages (`/admin/send`).
- A channel is enabled simply by having its credentials in `.env`.

---

## 0. Quickstart — Twilio Sandbox (no Meta approval needed)

1. Twilio Console → **Messaging → Try it out → Send a WhatsApp message**.
   From your phone, send the `join <code>` shown there to **+1 415 523 8886**.
2. Copy **Account SID** and **Auth Token** into `.env`
   (`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`). Leave `TWILIO_WHATSAPP_FROM=+14155238886`.
3. Run the server and a tunnel (see sections 3–4), then set
   `PUBLIC_BASE_URL=https://<your-ngrok-domain>` in `.env` and restart.
4. Sandbox settings → **When a message comes in**:
   `https://<your-ngrok-domain>/webhooks/twilio` (method **POST**). Save.
5. Message the sandbox number. Check `GET /` — it lists enabled channels.

Tip: `AGENT_MODE=echo` tests the plumbing without spending Claude tokens;
sending `fail` simulates an agent error so you can see the Slack escalation.

### Human handoff

- The bot escalates to Slack when the agent fails, a reply can't be delivered,
  or the user writes `humano` / `human` / `agente` / `persona` / `asesor`.
- While a user is in handoff the bot stays quiet and forwards their messages to Slack.
- Reply as a human (and optionally give them back to the bot):

```bash
curl -X POST "$URL/admin/reply" -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel":"twilio","to":"whatsapp:+521...","text":"Hola, soy Alan","release":true}'
```

---

## 0b. Slack threads (two-way, one thread per conversation)

With a Slack **app** (bot token) instead of the Incoming Webhook, every WhatsApp
conversation is mirrored into one thread in a channel, and you can answer the
customer from that thread. If `SLACK_BOT_TOKEN` is unset, the webhook behavior
above is unchanged.

**What you see in the channel**

- Parent: `💬 New chat — {name} (+{number}) · {channel}`. A new thread opens on
  the first message, or after `SLACK_THREAD_IDLE_HOURS` (24) without customer
  messages, i.e. when the WhatsApp customer-service window closed.
- Replies (silent, no mentions): `👤` customer, `🤖` bot, `🧑 admin:` replies sent via `/admin/reply`.
- Escalation (keyword, agent error, delivery failure): `🚨 @you escalated: {reason}`,
  broadcast to the channel so it surfaces outside the thread.
- `⏰ @you` once when a thread in human mode is <1h from the 24h window closing.

**Commands inside a conversation thread**

| Do this | Effect |
|---|---|
| type `take` (or ✋ `raised_hand` on the parent) | You take control; the bot pauses for that customer |
| any other reply while you have control | Sent to the customer on WhatsApp; ✅ on success, ⚠️ + note on failure |
| type `bot` (or ✅ `white_check_mark` on the parent) | Bot resumes |
| type `close` (or 🔒 `lock` on the parent) | Conversation closed: bot resumes, agent memory and transcript are cleared, and the customer's next message opens a new thread |

Commands are plain words, no `/` (Slack treats `/…` as slash commands).
Replies in a thread while the bot is in control are treated as internal notes
(not sent). Thread links (and who has control) are saved in SQLite
(`slack_threads`, see **Persistence**), so restarts and `--reload` keep existing threads
working, together with the agent's memory. A command typed in a thread that is no longer linked
gets a ⚠️ note pointing you to the newest thread.

**Slack app setup** (api.slack.com/apps → *Create New App* → From scratch)

1. *OAuth & Permissions* → Bot Token Scopes: `chat:write`, `channels:history`,
   `reactions:read`, `reactions:write` (for the ✅/⚠️ delivery marks), plus
   `groups:history` if the channel is private. *Install to Workspace* and copy
   the **Bot User OAuth Token** (`xoxb-…`) → `SLACK_BOT_TOKEN`.
2. *Basic Information* → **Signing Secret** → `SLACK_SIGNING_SECRET`.
3. *Event Subscriptions* → On. Request URL: `<PUBLIC_BASE_URL>/slack/events`
   (the server must be running; it answers Slack's `url_verification`).
   Subscribe to bot events: `message.channels` (or `message.groups` for a
   private channel) and `reaction_added`. Save; reinstall the app if Slack asks.
4. Channel ID: open the channel → channel name → *About* → bottom of the
   panel (`C0…`) → `SLACK_CHANNEL_ID`.
5. Your member ID: click your profile picture → *Profile* → ⋮ → *Copy member ID*
   (`U0…`) → `SLACK_ALERT_USER_ID`.
6. In the channel: `/invite @<your app name>`.
7. Restart the server; `GET /` should show `"slack_threads": true`.

Provider alerts (Meta account review etc.) keep going to `SLACK_WEBHOOK_URL`
if set; without a webhook they are posted top-level in the same channel.

---

## 0c. Human handoff in Twilio Flex (optional, `HANDOFF_TARGET=flex`)

```
user: "humano"  (or agent error)
   └─> bot creates Conversation CHxxx + adds user (whatsapp:+521…, proxy = your Twilio number)
   └─> bot creates Flex Interaction -> TaskRouter task (reason + transcript in attributes)
   └─> agent chats in Flex; the user's messages go to the Conversation, not to the bot
   └─> agent wraps up -> Conversation closes -> next message hits /webhooks/twilio -> bot is back
```

Setup:
1. Create a Flex instance (Console → Flex). **Use the same Twilio account** for
   the WhatsApp Sandbox/sender and Flex, and put that account's SID/token in `.env`.
2. Console → TaskRouter → Workspaces → **Flex Task Assignment**: copy the
   Workspace SID (`WS…`) and the workflow SID (`WW…`, e.g. "Assign to Anyone").
3. Keep the bot as the entry point. In Conversations → Defaults, leave
   **"Handle Inbound Messages with Conversations"** off. Make sure no Flex
   WhatsApp *address* (Flex → Messaging) points your number at a Studio flow,
   otherwise Flex grabs every message before the bot sees it.
4. `.env`: `HANDOFF_TARGET=flex`, `FLEX_WORKSPACE_SID`, `FLEX_WORKFLOW_SID`.
5. Log in to Flex as an agent, set yourself **Available**, and send `humano`
   from WhatsApp. The task shows up with `handoffReason` and `botTranscript` in its attributes.

Notes:
- Every Flex handoff also posts an FYI to Slack. If Flex fails (credentials,
  SIDs, API error), the handoff falls back to Slack automatically.
- Meta-channel users always go to Slack: Flex can only take conversations on Twilio numbers.
- `FLEX_CONVERSATION_CLOSE_TIMER` (default 24h) closes forgotten conversations
  so users don't get stuck away from the bot.
- Flex pricing: free active-user hours to start, then billed per active hour or per named user. Check Twilio's Flex pricing page.

---

## Pivot playbook

| Meta outcome | What to do | Code changes |
|---|---|---|
| **Approved** | Register the production number; put `WHATSAPP_*` + `META_APP_SECRET` in `.env`; point Meta's webhook at `/webhooks/meta`; set `WA_PROVIDER=meta`; update the link/QR people use to the new number | None |
| **Rejected, fixable** | Fix and resubmit verification; keep running on Twilio | None |
| **Rejected, stuck** | Note: a *production* sender on Twilio or any BSP still needs a verified Meta Business Portfolio, so a provider switch doesn't bypass this. Options: keep working with Meta on the rejection; run the demo on the Twilio Sandbox / Meta test number; or verify under a different legal entity | None |
| **Pending at demo time** | Twilio Sandbox, or Meta test number with volunteers pre-added | None |

**Get told when Meta decides:** in App Dashboard → WhatsApp → Configuration →
Webhook fields, subscribe to `account_review_update`, `account_update` and
`business_capability_update` (besides `messages`). They get posted to Slack.

---

## 1. Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/settings/keys)
- A free [Meta developer account](https://developers.facebook.com/)
- [ngrok](https://ngrok.com/download) (or any tool that gives you a public
  HTTPS URL pointing at your machine) — free tier is fine

---

## 2. Create the Meta app + WhatsApp test number

1. Go to https://developers.facebook.com/apps and click **Create App**.
   - App type: **Business**.
   - Give it any name (e.g. "whatsapp-dexter").
2. In the app dashboard, find **WhatsApp** in the left sidebar under
   "Add products to your app" and click **Set up**.
3. You land on **WhatsApp > API Setup**. Meta gives you, for free, without
   any business verification:
   - A **test phone number** you send messages *from* (Meta-owned).
   - A **Phone number ID** — copy this, it's `WHATSAPP_PHONE_NUMBER_ID`.
   - A **temporary access token** (valid 24h) — copy this, it's
     `WHATSAPP_ACCESS_TOKEN` for now. (Section 6 below covers getting a
     token that doesn't expire every day.)
4. On the same page, under **To**, click **Manage phone number list** and
   add your own WhatsApp number as a recipient. Meta will text you a code —
   confirm it. (Test numbers can only message up to 5 pre-approved
   recipients — plenty for a POC.)
5. Send yourself the "Hello World" template from that same page to confirm
   the number works before wiring up your own webhook.

---

## 3. Install and configure this project

```bash
cd whatsapp-dexter
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
```

Edit `.env`:
- `ANTHROPIC_API_KEY` — from the Anthropic console.
- `WHATSAPP_ACCESS_TOKEN` — the temporary token from step 2.3.
- `WHATSAPP_PHONE_NUMBER_ID` — from step 2.3.
- `WHATSAPP_VERIFY_TOKEN` — make up any random string; you'll paste the
  *same* value into the Meta dashboard in step 5.
- `ALLOWED_SENDERS` — optional; leave blank while testing solo.

---

## 4. Run the server and expose it publicly

**Shortcut (macOS):** `./run-local.sh` opens terminal tabs for uvicorn, ngrok
(reusing the ngrok domain in `PUBLIC_BASE_URL`) and a venv shell, then opens
the ngrok inspector, `GET /` and `/docs` in the browser and prints the webhook
URLs. `./run-local.sh --consoles` also opens the Meta/Slack/Twilio dashboards;
`./run-local.sh stop` stops everything. Manual steps:

```bash
uvicorn app.main:app --reload --port 8000
```

In a second terminal:

```bash
ngrok http 8000
```

ngrok prints a public URL like `https://abcd1234.ngrok-free.app` — that's
your base URL for the next step. Keep both terminals running.

---

## 5. Point Meta's webhook at your tunnel

1. In the Meta App Dashboard: **WhatsApp > Configuration**.
2. Click **Edit** next to Webhook and fill in:
   - **Callback URL**: `https://<your-ngrok-domain>/webhooks/meta` (legacy `/webhook` still works)
   - **Verify token**: the exact string you put in `WHATSAPP_VERIFY_TOKEN`
3. Click **Verify and save**. Meta immediately does a GET against your
   `/webhook` — you should see a `200` logged in your uvicorn terminal.
   If it fails, double check the verify token matches exactly and that
   uvicorn/ngrok are both still running.
4. Back on the Configuration page, under **Webhook fields**, click
   **Manage** and subscribe to **messages**. (This is the field that makes
   Meta actually forward inbound WhatsApp messages to you — easy to miss.)

---

## 6. Test it

From the WhatsApp number you registered as a recipient in step 2.4, send a
message to the Meta test number, e.g.:

```
What time is it in Mexico City right now?
```

You should see the webhook fire in your uvicorn logs, a `tool_use` log line
for `get_current_datetime`, and a reply arrive on WhatsApp within a few
seconds. Try `What's 234 * 18, plus 7?` to see the `calculate` tool fire.

Plain conversational messages (no tool needed) work too — the agent only
calls a tool when it decides one is useful.

---

## 7. Token expiry (temporary vs permanent)

The token from API Setup expires after 24 hours — fine for a first test,
annoying beyond that. To get a token that doesn't expire daily:

1. **Business Settings > Users > System Users** — create a system user.
2. Assign it the WhatsApp app with `whatsapp_business_messaging` +
   `whatsapp_business_management` permissions.
3. Generate a token for that system user with no expiry (or a long one),
   and drop it into `WHATSAPP_ACCESS_TOKEN`.

---

## 8. Where to go from here

- **Persistence**: state is in SQLite (see **Persistence**); move to Postgres only if you
  ever need more than one worker or host.
- **More tools**: add real business logic in `app/agent.py` (`TOOL_DEFINITIONS` and `run_tool`) following the
  `get_current_datetime` / `calculate` pattern (register in
  `TOOL_DEFINITIONS`, implement, wire into `run_tool`).
- **Media messages**: `parse_webhook_payload` currently only extracts text;
  extend it for images/audio/documents if you need those.
- **Production**: move off ngrok to a real host with a stable HTTPS
  endpoint, add signature verification on `/webhook`
  (`X-Hub-Signature-256`) and structured logging/observability (rate limiting and
  cost caps are in place, see **Limits**).
- **Migrate to the real API**: everything here is the *same* Cloud API
  you'd use in production — moving off the "test number" mainly means
  verifying a business and requesting a real phone number.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest                      # unit + offline evals, no network
```

Live evals against the real models are opt-in (see **Evals**).

---

## CI/CD

Every pull request and every push to `main` runs the tests (`.github/workflows/ci.yml`, check name **`ci`**).
Every push to `main` (except changes that only touch `**.md` / `docs/**`) also deploys to the Droplet
(`.github/workflows/deploy.yml`):

```
merge to main ──> test (pytest, same commit) ──> deploy job (environment: production, one at a time)
                       │ fail: no deploy             │
                       ▼                             ├─ rsync code ──> /opt/whatsapp-bot   (.env, data/, backups untouched)
                    Slack ❌                         ├─ ssh deploy@droplet deploy/remote-deploy.sh <sha>
                                                     │     build whatsapp-bot:<sha> → :current (old one kept as :previous)
                                                     │     up -d → GET / inside the container until "status":"ok" (60 s)
                                                     │     unhealthy → :previous → :current, up -d, job fails
                                                     ├─ GET https://origin.alanestrada.space/ (retries)      
                                                     ▼
                                               Slack ✅ / ❌ (+ link to the run)
```

- The image is built **on the Droplet**, as before (no registry). If builds get too heavy for 1 GB RAM,
  the next step would be to build in Actions, push to GHCR and only `docker pull` on the server.
- The server keeps the last 3 builds (`whatsapp-bot:<sha>`) plus `:current` and `:previous`, and prunes the rest.
  `/opt/whatsapp-bot/DEPLOYED_SHA` holds the commit that's live.
- App secrets **never** go to GitHub. `.env` lives only on the server; neither CI nor `deploy.sh` overwrites it
  unless you ask (see below). `deploy/rsync-excludes` lists what is never sent nor deleted on the server.
- Live evals (`eval_live` marker) never run in CI; tests use dummy values and mock all HTTP.

**GitHub secrets** (repo → Settings → Secrets and variables → Actions)

| Name | Kind | Value |
|---|---|---|
| `DEPLOY_SSH_KEY` | secret | Private key `~/.ssh/whatsapp_bot_ci` (whole file, including the BEGIN/END lines) |
| `DEPLOY_KNOWN_HOSTS` | secret | Output of `ssh-keyscan -t ed25519 <droplet IP>` (pins the host key) |
| `DEPLOY_HOST` | secret | Droplet IP (same value you used for `ssh-keyscan`) |
| `SLACK_DEPLOY_WEBHOOK` | secret, optional | Slack Incoming Webhook URL for deploy messages; unset = no Slack |
| `SLACK_ALERT_USER_ID` | **variable**, optional | Your Slack member ID (`U0…`), mentioned on failures |
| `PUBLIC_HEALTH_URL` | **variable**, optional | URL the deploy checks for `"status":"ok"` (default `https://origin.alanestrada.space/`; the bare `www…/whatsapp-bot/api/` is a Vercel 404, only sub-paths are forwarded) |

**Redeploy on demand:** Actions → **deploy** → **Run workflow** → pick the branch. It always deploys
(tests still run first). Useful after editing `.env` on the server, or to deploy a branch to try it.

**Roll back by hand** (on the server):

```bash
ssh whatsapp-bot
cd /opt/whatsapp-bot/deploy
IMAGE_TAG=previous docker compose up -d --no-build --force-recreate app   # the build before the current one
docker image ls whatsapp-bot                                              # or pick any kept build:
IMAGE_TAG=<sha> docker compose up -d --no-build --force-recreate app
```

This lasts until the next deploy. To make it stick, `git revert` the bad commit and merge.

**Change `.env`:**
- edit it on the server (`ssh whatsapp-bot`, `nano /opt/whatsapp-bot/.env`), then run the deploy workflow from
  the Actions tab, or on the server: `cd /opt/whatsapp-bot/deploy && docker compose up -d --no-build --force-recreate app`; or
- from the Mac: `./deploy/deploy.sh --with-env` (pushes your local `.env` and deploys).

**Manual deploy from the Mac** still works and runs the same server script (build, health check, rollback):
`./deploy/deploy.sh` (code only), `--with-env`, or `--dry-run` (shows what rsync would send or delete).
Uncommitted changes are tagged `<sha>-dirty-<time>`.

**One-time setup** (in order):
1. Mac: `ssh-keygen -t ed25519 -f ~/.ssh/whatsapp_bot_ci -N "" -C "github-actions-deploy"`.
2. Droplet: `ssh whatsapp-bot "bash -s -- '$(cat ~/.ssh/whatsapp_bot_ci.pub)'" < deploy/setup-ci-user.sh`
   (creates user `deploy` in group `docker`, installs the key, `chown`s `/opt/whatsapp-bot`, turns off SSH
   passwords while keeping root key login). Check `ssh whatsapp-bot true` and
   `ssh -i ~/.ssh/whatsapp_bot_ci deploy@<IP> docker ps` both work. Note: group `docker` is root-equivalent.
3. Add the secrets above. 4. Settings → Environments → create `production` (add reviewers there later if you
   want approval before deploys). 5. Settings → Branches → protect `main`, require the `ci` status check.

---

## Project layout

```
app/
  main.py              Webhooks (/webhooks/twilio, /webhooks/meta, /slack/events), core flow, /admin/*
  channels/
    base.py            Channel protocol, InboundMessage, normalize_user_id
    twilio.py          Twilio WhatsApp: signature check, parse, send
    meta.py            Meta Cloud API: signature check, parse (+ account events), send
    __init__.py        Registry: enabled channels, WA_PROVIDER default
  agent.py             Claude tool-use loop, tool definitions and implementations, EchoAgent (AGENT_MODE=echo)
  db.py                SQLite: connection, migrations, dedupe ids, flags, retention purge
  limits.py            Usage per user/day, rate + token caps, /admin/usage summary
  analytics/           Metrics & governance: events.py (event log, samples, llm_calls), topics.py,
                       pricing.py, metrics.py (KPIs), digest.py (Slack), anthropic_usage.py
                       (Admin API reconciliation), mcp_server.py (read-only MCP connector + OAuth)
  escalation.py        Slack escalation + provider event alerts (Incoming Webhook)
  slack_bridge.py      Slack app bridge: thread per conversation, take/bot, /slack/events
  handoff.py           Handoff router: Flex (Conversations + Interactions API) or Slack
  twilio_api.py        Minimal Twilio REST helper
  resilience.py        Retry with exponential backoff
  intents.py           Word lists: greetings, info requests, goodbyes, acknowledgements
  on_request.py        On-request entries: load, trigger matching, unlocks, lookup tool
  leads/               Leads: consent, scoring, Slack speed-to-lead, templates, Zoho, dashboard, web form
  status.py            Live status for on-request entries (build info, uptime, features, Meta state)
  kb/on_request/       Entries Dexter only shares when explicitly asked (see its README.md)
  docs/                PDF generation (reportlab): slides.py, topic.py, assessment.py, dossier.py, common/theme
  store.py             History, language, handoff, transcript, webhook dedupe, Slack thread map
                       (in memory, written through to SQLite)
  config.py            Settings loaded from .env
evals/                 Eval set: cases/*.json + harness.py (offline in pytest, live opt-in)
scripts/               transcript_to_case.py (real conversation -> draft eval case)
tests/                 pytest suite (no network, HTTP mocked with respx)
```

## Talk companion flows ("Gobernanza de IA")

With `FLOWS_ENABLED=true` (default) the bot is the companion for Alan's talk:

| Flow | Trigger | How it works |
|---|---|---|
| Welcome menu | greeting (`hola`, `menu`, …), or after the first question | WhatsApp list message on Meta; numbered text elsewhere (`1`–`5`) |
| Explore the talk | menu 1 or any question | Claude answers from `app/kb/core/*.md` (whole, prompt-cached) and searches `app/kb/sources/*` with the `search_sources` tool (BM25, no vector DB) |
| AI maturity assessment | menu 2, `diagnóstico`/`quiz`, or the agent's `start_assessment` tool | Deterministic (`app/flows/quiz.py`): 9 questions on the talk's 7 domains + adoption + measurement, L0–L3, cap at L1 without sponsorship/ownership, 3 next steps. Result note goes to the Slack thread. `salir`/`exit` stops it. If the person first describes their company (typed or by voice note), the agent pre-scores the domains their story clearly covers and the quiz asks only the rest (see **Voice notes and assessments from a story**) |
| About Alan | menu 3 | Agent answers from `app/kb/core/about.md` |
| Talk to Alan | menu 4, `humano`, or the agent's `talk_to_alan` tool | Slack handoff (existing) |
| Slides / PDFs | menu 5, `lámina 8`, `slides`, `pdf`, or the agent's `send_slides` / `create_topic_pdf` / `send_assessment_report` tools | See **PDF documents** below |

Language follows the user (ES/EN). To update content, edit the Markdown in `app/kb/core/` (and drop extra `.txt`/`.md` sources in `app/kb/sources/`), then redeploy.

## PDF documents

The bot sends three kinds of PDF as WhatsApp **document** messages (Meta channel). They are generated in
pure Python with reportlab and the PDF built-in fonts. Single-slide images are rendered from the same PDF page with pypdfium2 (pinned in `requirements.txt`).

| Document | Who asks | Content |
|---|---|---|
| **Slides** (whole deck, 13 pages) | menu 5, `slides`, `láminas`, or the agent | 10 slides + annex A (change management) + annex B (governance frameworks) + a "first 90 days" page, 16:9 |
| **A single slide** (sent as an **image** by default, which previews inline; add `pdf` for a PDF: `lámina 8 pdf`) | `lámina 8`, `slide A`, or the agent ("send me the slide about measurement") | The same page as in the deck. Ids: `1`–`10`, `A`, `B`, `90` |
| **Topic one-pager** | the agent's `create_topic_pdf` ("send me a PDF about the 10-20-70 rule") | Claude writes title, summary, sections, key figures and sources **from the knowledge base**; the layout is fixed. Max ~520 words, ~2 pages; longer content is bounced back to the agent to shorten |
| **Assessment report** (3 pages) | sent automatically when the quiz ends; `pdf` / `reporte` re-sends it | Level and score, position on L0–L3, strengths and priorities, every domain with the answer the person picked, next steps for the weakest domains (why it matters + what to do), a 90-day checklist |

How delivery works (`MetaChannel.send_document`): the PDF is uploaded to `POST /{phone_number_id}/media`, then sent by
**media id**. No public file URL is needed, so a person's assessment report is never exposed on a link. Same 24 h
customer-service window as text. If the upload or send fails, the user gets a short text instead and the Slack thread
gets a note; the chat keeps working. Documents sent are mirrored to the Slack thread as `📎 filename`.
Twilio can't send files in this build: explicit requests get a polite text, and the automatic quiz PDF is skipped silently.

Content sources and how to change them:
- **Slides**: `SLIDES` in `app/docs/slides.py` (mirrors `app/kb/core/platica.md`). Spanish only. They are rebuilt from the
  talk outline, not exported from your real deck: edit them when the talk changes, or swap the file for your own PDF.
- **Assessment report**: questions, levels and tips come from `app/flows/quiz.py`; the per-domain "why it matters" lines
  (`WHY`) and the 90-day list are in `app/docs/assessment.py`. Report language follows the quiz language (ES/EN).
- **Topic one-pager**: no fixed content; the agent writes it. Limits are in `app/docs/topic.py` (`LIMITS`, `MAX_WORDS`).
- Look and feel (colors, fonts, footer): `app/docs/theme.py`.

## On-request information

Information stored inside Dexter that it gives **only to people who explicitly ask for it**:
never a menu option, never suggested, never mentioned in "what can you do?" answers. The first
entry, `app/kb/on_request/dexter-making-of.md`, is the making of Dexter (hosting, domain, Meta
setup and verification, why not Twilio, costs, lessons, the repo) with Dexter's **live status**.
It's a reward for curious attendees: ask "¿cómo se hizo Dexter?" / "how was Dexter built?".

- **Hidden by construction**: entries are not in the system prompt, the core KB, the
  `search_sources` index or the menu. The model can't suggest what it can't see.
- **Fast path** (`main._route_flows`, before the guard and the agent): the message matches an
  entry's `triggers` → its `## Respuesta` (or `## Response` for English) is sent as written.
  No model call, so it works even when the token budget is spent.
- **Agent path**: a generic `lookup_private_info(query)` tool (its description names no topic).
  The server runs the same matcher on the query and returns the entry or `found: false`.
- **Matching** is deterministic and only looks at triggers (accents/punctuation ignored; a whole
  trigger phrase scores 1.0; otherwise the share of the trigger's meaningful words, discounted
  when the message is about something else). Content is never searched.
- **After disclosure** the entry is unlocked for that conversation only: `## Detalle` joins
  that user's system prompt (after the cached prefix) so follow-ups work, the off-topic guard
  treats follow-ups as in scope, and `pdf` sends the whole entry as a multipage PDF
  (`app/docs/dossier.py`). Closing the chat locks it again. A silent `🔑 On-request info
  shared: <id>` note goes to the Slack thread. Human mode: nothing is auto-disclosed.
- **Live status** (`{{status_line}}`, `{{status}}`, `app/status.py`): commit and deploy date and
  passing tests and eval count from `app/BUILD_INFO.json` (written by `deploy/remote-deploy.sh`,
  test count from CI), uptime, ✅/⏳ features from the actual config, Meta verification state
  from the last account webhooks (fallback `META_VERIFICATION_STATUS`), and counters
  (conversations, assessments, curious people). No IPs, ids, phone numbers or user data;
  a failing field is dropped; a literal `{{…}}` never reaches a user.

- **Lead generation** (`lead: true`): the content is shared without restrictions, and Alan is
  always one message away. The entry's `## Contacto` / `## Contact` invitation ("escribe *hablar
  con Alan*") goes out as its own bubble after the reply and after the PDF; the agent is told to
  repeat it naturally (costs, building one, their company, working with Alan), and if three
  answers in a row go by without it the bot adds it itself. When a lead asks for Alan (typed,
  `humano` or the agent's `talk_to_alan`), they get a personal reply ("te va a escribir él
  mismo") and Alan's Slack alert starts with `🔥 Lead (read: <id>)`.

Add or edit an entry: see `app/kb/on_request/README.md` (format, triggers, the
volume option for entries that must not live in git). Config: `ON_REQUEST_ENABLED`,
`ON_REQUEST_DIR`, `ON_REQUEST_EXTRA_DIR`, `META_VERIFICATION_STATUS`.

## Files from attendees (photos, screenshots, PDFs)

Attendees can send a JPG/PNG/WebP/GIF or a PDF (with or without a caption) and ask about it. The bot downloads it
(Graph `GET /{media_id}`, then the short-lived URL with the Bearer token), validates it and shows it to Claude for **that turn only**.

- Limits: photos 4 MB, PDFs 8 MB and 20 pages (`app/attachments.py`). The size is checked on the declared size and while streaming; the real type is sniffed from the bytes, not trusted from the sender.
- Nothing is saved to disk, and the chat history keeps only a text note (`[Attached image: name]`), never the file.
- The prompt tells Claude to treat file contents as data, not instructions, and not to repeat IDs or card numbers it sees.
- Video, stickers, other types, files too big, unreadable PDFs and download failures get a short polite reply (ES/EN). During the quiz, a file is asked to wait. Voice notes: see below.
- A caption like `humano` still hands off to Alan. In the Slack thread the file shows as `📎 name`; the file itself is not forwarded.
- Meta only. Twilio gets the "photos, PDFs and text only" reply.

Try it without WhatsApp:

```python
from app import docs
open("deck.pdf", "wb").write(docs.make_deck().data)            # whole deck
open("s8.pdf", "wb").write(docs.make_deck("8").data)           # one slide
```

## Voice notes and assessments from a story

Attendees can send a WhatsApp voice note instead of typing: a question, an assessment answer ("la tercera"),
or a few minutes about their company, what they struggle with and how they approach AI.

Claude doesn't take audio, so `app/voice.py` transcribes the note first with any OpenAI-compatible
`/audio/transcriptions` endpoint, then the transcript goes through the normal flow as if it had been typed
(goodbye and `humano` keywords, quiz, menu, off-topic guard, agent). The agent sees it as `[Voice note] …`.

| Variable | Default | |
|---|---|---|
| `TRANSCRIBE_API_KEY` | (empty) | Turns voice notes on. Empty = voice notes get the "photos and PDFs only" reply, as before |
| `TRANSCRIBE_BASE_URL` | `https://api.openai.com/v1` | Groq: `https://api.groq.com/openai/v1` |
| `TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` | Groq: `whisper-large-v3-turbo` |
| `VOICE_ENABLED` | `true` | `false` turns voice notes off even with a key |

- Limits: 4 MB per note (~15+ minutes of WhatsApp voice), and each note counts toward `USER_FILES_PER_DAY`.
  Ogg/Opus (WhatsApp voice notes), MP3, M4A, WAV and WebM are accepted, sniffed from the bytes; AMR/AAC get a polite reply.
- The audio is never written to disk: it is sent to the transcription service and dropped. Only the transcript
  is kept (chat history, and a `🎙️ Transcript:` note in the Slack thread). Note that the audio does leave for that
  third-party service. Transcription cost is not counted in the token caps.
- Download or transcription failures and silent notes get a short ES/EN reply asking to resend or type it.
- Meta only, like photos and PDFs.

**Assessment from a story.** When someone describes their company (by voice or text) and wants a diagnostic, the
agent calls `start_assessment` with `known`: a 0–3 score for each domain their account *clearly* supports, on
the quiz's own answer scale (the options are in the tool description, generated from `app/flows/quiz.py`).
The quiz then lists what it scored ("Con lo que me contaste ya califiqué 4 de 9 dominios: …") and asks only
the missing domains; with all 9 covered the result and PDF arrive right away. Scoring is unchanged. The result
text, the PDF and the Slack note mark the domains that came from the person's account. If someone only shares
their story, the agent reflects it back in light of the talk and offers to turn it into the diagnostic.
If a pre-scored domain looks wrong, the person types `salir` and asks for the full assessment.

## Conversation lifecycle & guardrails

| Situation | What the user gets | Config |
|---|---|---|
| User says goodbye or signals they're done (see [Ending a conversation](#ending-a-conversation)) | One farewell + "escribe *hola*"; memory wiped; next message starts fresh with the menu | `CLOSE_ON_INTENT=true` |
| No activity | "¿Sigues ahí?" warning, then a closing message | `IDLE_WARN_MINUTES=10`, `IDLE_CLOSE_MINUTES=5` (chats with a human in charge are skipped) |
| Alan closes from Slack (`close` or 🔒 on the parent) or `POST /admin/close` | "Alan cerró esta conversación…" | — |
| Server restart | Nothing: history, quiz progress, mode and the Slack thread come back from SQLite and the chat simply continues | `DB_PATH` |
| Server restart and a chat's state could **not** be recovered (DB unreadable, a failed write) | Apology from Alan saying the history was lost — sent on startup to chats active in the last 23 h, or before the reply to their next message | `RESTART_NOTICE=true` |
| Message off the talk's topic | A cheap classifier (`GUARD_MODEL`, Haiku) runs first; off-topic gets a static warning "Aviso 1 de 2 / 2 de 2" with no main-agent call; the next one hands the chat to Alan. The first message of a chat is never a warning: it gets the welcome and the menu, and the count stays at 0 | `GUARD_ENABLED`, `OFFTOPIC_MAX_WARNINGS=2` |
| Opening info request ("Hola, quisiera más información", "más info", "what can you do?") | The welcome and the menu, with no classifier and no model call | — |

All of this state lives in `data/bot.sqlite3` on the `app_data` Docker volume, so it survives redeploys. Messages of ≤ 3 words skip the classifier (follow-ups like "¿y en ventas?"). The guard fails open: if the classifier errors, the main agent answers.


### Ending a conversation

The bot closes as soon as the user is done, instead of waiting for the idle timeout. Every close goes through one routine (`close_chat` in `app/main.py`, reason `user_keyword | user_intent | user_intent_late | idle | admin`): send the closing message, wipe agent memory and quiz progress, clear the idle timers, persist the session, and post a silent note in the Slack thread (`🔒 Closed by user: "<what they wrote>"`). The next message starts fresh with the menu.

1. **Keyword fast path (zero tokens).** Runs before the usage caps, the off-topic guard and the agent. The *whole* message must be a closer (punctuation and common emoji stripped): the classic `adiós`, `bye`, `hasta luego`, `nos vemos`, `terminar`…, plus `sería todo`, `es todo`, `eso es todo`, `nada más`, `ya quedó`, `listo, gracias`, `con eso`, `that's all`, `that's it`, `I'm good, thanks`, `no thanks`. Those close as `user_keyword`. **Composite closers** close on the same path, as `user_intent`: a message made only of thanks, farewell and time-of-day words, with at least one farewell or time-of-day marker (`gracias, buen día`, `muchas gracias, buenas noches`, `gracias que tengas bonito día`, `thanks, have a good day`). A bare `gracias` does not close, and neither does `buenas noches, tengo una pregunta`. A composite is never a chat's first message (`buenas tardes` as the opener is the menu). Questions (`¿es todo?`) and longer messages go to the agent. The vocabulary lives in `app/intents.py`.
2. **Agent tool `end_conversation`.** For everything else ("perfecto, con esto ya tengo lo que necesitaba"), the agent decides from the history. When it calls the tool, its own text for that turn is discarded and the user gets one message: the tool's `farewell` + the re-engage line (`¡Un gusto! Que tengas excelente día. 👋 Cuando quieras, escribe *hola* para empezar de nuevo.`), or the standard closing message if `farewell` is empty. No further model call happens in that turn.
3. **Ambiguous replies: ask, then close.** A bare `gracias`, `ok`, `perfecto`, `listo`, 👍, or a goodbye the agent is not sure of, does not close by keyword. The agent answers with only "¿Algo más en lo que te pueda ayudar?" (in English, "Anything else I can help with?"). For 15 minutes after that question (`ASK_WINDOW`), a bare `no`, `no gracias`, `nada`, `es todo`, `nope`, `no thanks`, `gracias`, `ok` or 👍 closes the chat on the keyword path, with zero tokens (`user_intent`). Any other message goes to the normal flow, and any other message the bot sends clears the question. After the window, a `no` goes to the agent, which calls `end_conversation`. Messages that contain a closer ("no, eso sería todo por hoy, gracias") skip the off-topic classifier, so a goodbye is never counted as off-topic.
4. **Quiz.** `salir` (or `exit`, `cancelar`, `terminar`, `menú`) only stops the quiz. Any other closer (`es todo`, `adiós`) mid-quiz stops the quiz and closes the chat. A bare `no` is a quiz answer (invalid input), not a close.
5. **Human handoff.** While Alan is in charge nothing closes automatically: the message is mirrored to Slack and Alan closes with `close` / 🔒.
6. **Idle backstop, softer after a thanks.** `IDLE_WARN_MINUTES` / `IDLE_CLOSE_MINUTES` still close quiet chats, and a closed chat never gets the "¿Sigues ahí?" warning. If the user's last message was a bare thanks, ok or no, and the bot had asked "¿Algo más?", the chat closes at `IDLE_WARN_MINUTES` with the normal farewell and no warning. Any idle close after a goodbye or a bare thanks is recorded as `user_intent_late`: a close, not a gap (see "Metrics & governance").

`CLOSE_ON_INTENT=false` restores the previous behaviour: only the classic goodbye words close (never inside the quiz), and the agent has no `end_conversation` tool. Composite closers and ask-then-close go too.

### Reply length

The prompt asks for about `REPLY_MAX_WORDS` words (120 by default): 2–3 short paragraphs (each one is a WhatsApp bubble) or up to 5 short bullets, the core first, then one concrete offer to go deeper on a named sub-point, or the topic's one-page PDF. WhatsApp formatting only (`*bold*`, no headers or tables).

Code enforces the limit too (`app/formatting.py`, `cap_words`), on the text sent and never on tool inputs:

- **Hard cap (`REPLY_MAX_WORDS_HARD`, 220 by default).** A longer reply is cut at the last complete paragraph under the cap (or at a sentence, if the first paragraph is already too long), and the standard offer line is appended: `¿Quieres que profundice en algún punto?` / `Want me to go deeper on any part?`. A reply the model cut off at `max_tokens` gets the same treatment, minus its incomplete sentence. Nothing is ever cut mid-sentence.
- **Output budget.** `MAX_TOKENS = 2048` in `app/agent.py`, so a `create_topic_pdf` input (about 520 words) fits. The cap never touches a tool's input.
- **Metrics.** Every `agent_turn` event carries `reply_words` and `trimmed`. The overview (`GET /admin/metrics`, the MCP `get_overview`) and the daily digest report the mean and p90 reply words and the trim rate. The `reply_length` anomaly flags a day with more than 20 agent turns whose p90 is over 1.5 × `REPLY_MAX_WORDS`.

## Persistence

Everything the bot needs to continue a chat is in **`data/bot.sqlite3`** (`DB_PATH`), on the
`app_data` volume in Docker (`/srv/data`), so restarts and redeploys don't lose conversations.
Stdlib `sqlite3`, WAL mode, one connection behind a lock (single uvicorn worker). The working
copy stays in memory; every change is written through when it happens, and startup reads it
all back. A DB error never breaks a WhatsApp reply: it is logged, Slack gets one alert, and the
turn continues from memory.

| Table | What | Notes |
|---|---|---|
| `conversations` | channel, reply_to, profile name, mode (`bot`/`slack`/`flex`), language, plain-text transcript | one row per user |
| `messages` | agent history, exactly what is sent to the API | text only, never file bytes; capped at `HISTORY_MAX_MESSAGES` (40), trimmed oldest first without splitting a tool_use/tool_result pair |
| `quiz_state`, `quiz_results` | quiz in progress, last result | a redeploy mid-quiz just continues |
| `sessions` | lifecycle record (open/closed, idle warning, off-topic count, boot id) | |
| `slack_threads` | thread per conversation, mode, last customer message | unique index on `thread_ts` |
| `processed_messages` | Meta/Twilio message ids and Slack event ids (`meta:…`, `twilio:…`, `slack:…`) | dedupe, 7 days |
| `usage` | requests, files, input/output/cache tokens per user per day | see **Limits** |
| `flags` | one-time markers (limit notice sent today, budget alert sent) | |
| `schema_version` | last migration applied | migrations are an ordered list in `app/db.py`; append, never edit |

- **Closing** a conversation (user, idle, Slack `close`, `/admin/close`) deletes that user's history and quiz progress in one transaction.
- **Retention**: on startup and once a day, conversations closed or idle for more than `RETENTION_DAYS` (30) are deleted from every table, plus processed ids older than 7 days (`PROCESSED_RETENTION_DAYS`) and old usage rows.
- **Upgrade from the JSON files**: on the first start, if `data/sessions.json` / the Slack thread JSON (`SLACK_STATE_FILE`) exist and the tables are empty, they are imported and renamed to `*.imported`. Those chats had no saved history, so they still get the restart apology once.
- **Restart apology**: only for open chats whose history or quiz could not be recovered. The code path and `RESTART_NOTICE` stay for that case.

Backup (safe while the bot runs, thanks to WAL):

```bash
sqlite3 data/bot.sqlite3 ".backup data/bot-$(date +%F).sqlite3"           # local, with the sqlite3 CLI
# On the droplet (the image has no sqlite3 CLI; Python's stdlib does the same):
cd /opt/whatsapp-bot/deploy && docker compose exec app python -c \
  "import sqlite3; sqlite3.connect('data/bot.sqlite3').backup(sqlite3.connect('data/backup.sqlite3'))"
docker compose cp app:/srv/data/backup.sqlite3 ./bot-backup-$(date +%F).sqlite3
```

## Limits

Usage of every model call (main agent and guard) is recorded per user per day
(America/Mexico_City), including cache tokens. Caps (`LIMITS_ENABLED=true` by default):

| Variable | Default | Counts | When hit |
|---|---|---|---|
| `USER_MSG_PER_MINUTE` | 6 | inbound messages per user, rolling minute | "Recibí muchos mensajes seguidos; dame un minuto 🙂", once per burst |
| `USER_MSG_PER_DAY` | 120 | inbound messages per user per day | daily-limit note + offer to type *humano*, once a day |
| `USER_FILES_PER_DAY` | 10 | inbound photos/PDFs/voice notes per user per day | file isn't downloaded; note once a day |
| `USER_TOKENS_PER_DAY` | 300000 | input+output tokens per user, all model calls | no more model calls for that user today; note once a day |
| `DAILY_TOKEN_BUDGET` | 5000000 | input+output tokens, everyone | 80%: one top-level Slack alert mentioning `SLACK_ALERT_USER_ID`; 100%: model calls stop for everyone (another alert) |

- Limit replies are static ES/EN text (no model call); the Slack thread gets a silent ⏳ note.
- Order: `adiós` / `Sería todo` and `humano` always work; then the message caps; then menu, quiz and slides, which keep working when a **token** cap is hit (only the guard and the agent are blocked).
- Quiz answers don't count against the message caps, and a chat in human-handoff mode is never limited (messages still mirror to Slack).
- The per-minute window is in memory (a restart resets it); daily counts are in SQLite.
- `LIMITS_ENABLED=false` behaves exactly as before (usage is still recorded).

```bash
curl -s "$URL/admin/usage" -H "X-Admin-Token: $ADMIN_TOKEN"
# {"day": "2026-10-10", "totals": {"requests": 812, "tokens": 1430221, ...},
#  "budget": {"daily_token_budget": 5000000, "used_pct": 28.6},
#  "top_users": [{"user": "****5678", "tokens": 41210, ...}, ...]}   (numbers masked)
```

## Metrics & governance

Alan governs Dexter by **asking Claude in chat** instead of reading a dashboard: an event log,
topic labels and per-call cost land in SQLite; a daily digest goes to Slack; a read-only MCP
connector lets Claude query the same numbers live. All of it is in `app/analytics/`, and
analytics never breaks or delays a WhatsApp reply (every write is caught, logged and skipped).
`ANALYTICS_ENABLED=false` writes nothing and the bot behaves exactly as before.

### Event model

`events(id, ts, user_id, conversation_id, type, step, data_json)`. `ts` is UTC ISO-8601. Writes
are fire-and-forget on one background thread. `conversation_id` is new on every session start,
including the first message after a close. It lives in the session state, so it survives restarts.
Every close records `last_step`, the most recent funnel step.

| type | where | data |
|---|---|---|
| `conversation_start` | first message of a session | `source` (`ENTRY_SOURCES` or `organic`), `lang`, `channel` |
| `menu_shown` / `menu_choice` | welcome menu / talk overview, menu option | `menu` / `option` |
| `quiz_start` / `quiz_answer` / `quiz_complete` / `quiz_exit` | `flows/quiz.py` | `q` (1–9), `level`; never the answer text. Step `quiz_qN` = question N on screen |
| `report_sent`, `slides_sent`, `topic_pdf_sent` | file delivered | `pages`, `slide`, `format` |
| `agent_turn` | after each agent turn | `tools`, `steps`, `latency_ms`, `input_tokens`, `output_tokens`, `topic`, `outcome` (`answered`/`handoff`/`error`/`closed`) |
| `file_in` | inbound photo/PDF/voice note | `kind`, `accepted`, `reject_reason` |
| `offtopic_warning` | guard said OUT | `n` (1/2) |
| `handoff` | `hand_off()` (+ delivery failures) | `reason`: `user_asked`/`agent_error`/`offtopic`/`delivery_failure`/`limit`/`other` |
| `human_take` / `human_release` | Slack bridge | — |
| `limit_hit` | any limit | `limit` |
| `error` | agent, guard, sends | `kind` (exception class only), `where` |
| `msg_status` | Meta `statuses` callbacks (never reach `handle_inbound`) | `status`, `error_code`, `wamid` |
| `user_msg` | every inbound message | `topic`, `route`, `lang`, `kind` |
| `gap` | see below | `topic`, `source` (`tool`/`handoff`/`idle_after_answer`), `sample_id`, `note_id` |
| `csat` | 👍/👎 after a user close (`CSAT_ENABLED`) | `value` (`up`/`down`) |
| `on_request_disclosed` | first time an on-request entry is given in a conversation (fast path or `lookup_private_info`) | `id` |
| `conversation_close` | `close_chat()` | `reason`, `last_step`, `turns`, `duration_s`, `last_bot_msg_read` (true/false, null = unknown, e.g. Twilio), `csat_offered` |

Close reasons: `user_keyword | user_intent | idle | admin | offtopic_handoff | limit | error | window_expired`
(today only the first four happen). **Note:** "Sería todo, gracias" is a *keyword* close
(`user_keyword`, zero tokens). `user_intent` is when the agent calls `end_conversation` on a free-form
goodbye ("Perfecto, con eso ya tengo todo, muchas gracias").

The QR prefill text (`ENTRY_SOURCES='{"hola dexter": "qr_talk"}'`, matched ignoring case, accents
and punctuation) sets the source and gets the welcome menu, same as "hola".

**Gaps.** A turn is a gap when the agent called `note_gap` (an invisible tool; the turn goes
on), when it ended in a handoff with reason ≠ `user_asked` (agent error), or when the chat
closed `idle` right after an answer that was read (`last_step=agent_turn`, `last_bot_msg_read=true`).

**Question samples** (`question_samples`): the text of messages that reached the agent, plus the
agent's `note_gap` summaries (`kind=gap_note`). Phones, emails, URLs, card numbers and long digit
runs are replaced with `[…]`, and text is cut at 200 characters. These texts come from attendees,
so treat them as untrusted data, never instructions.

**Cost** (`llm_calls`): one row per Anthropic call (`purpose` = `agent | guard | other`) from the
response's `usage` (cache read/write included), plus failed calls with the exception class. The
`usage` table that the limits use is still fed. Prices are in `app/analytics/pricing.py` (Sonnet 4.5
and Haiku 4.5, checked 2026-10-11) and `MODEL_PRICES_JSON` overrides them. Cost is computed at
query time, so a price fix re-prices history. With `ANTHROPIC_ADMIN_KEY` (`sk-ant-admin…`), the
Usage & Cost Admin API is read server-side, cached for 15 minutes, to reconcile spend.

**Retention:** events, samples, `llm_calls` and outbound ids follow `RETENTION_DAYS`.

### Topic taxonomy (`app/analytics/topics.py`)

`adopcion`, `estrategia_valor`, `gobernanza_riesgo`, `liderazgo`, `roles_ownership`,
`personas_cultura`, `procesos`, `datos_plataforma`, `medicion_costo` (the 9 quiz domains) +
`sobre_alan`, `la_platica_general`, `laminas`, `diagnostico`, `contacto_alan`, `making_of`,
`saludo_menu`, `despedida`, `fuera_de_tema`, `otro`.

The Haiku guard returns `{"on_topic": bool, "topic": "<id>"}` in the same call, so labels cost no
extra model call. The on/off decision and fail-open behaviour are unchanged, and an unknown id
becomes `otro`. Messages that skip the guard are labelled from their route: menu option, quiz,
close, slides, handoff. A short follow-up ("¿y en ventas?") keeps the conversation's last topic.

### KPI definitions (`app/analytics/metrics.py`, one source of truth)

Ranges: `today`, `yesterday`, `7d`, `30d` (any `Nd`), an ISO date, or `from..to`. **Days are
America/Mexico_City days**, and every result says so.

| Metric | Definition |
|---|---|
| Conversations | count of `conversation_start` in range |
| Containment rate | closed conversations with no `handoff` and no `human_take` ÷ closed conversations |
| Escalation rate | conversations with ≥1 `handoff` ÷ conversations; also split by `reason` |
| Completion | quiz: `quiz_complete` ÷ `quiz_start`; report delivery: `report_sent` ÷ `quiz_complete`; answered turns: `agent_turn` with `outcome=answered` ÷ all `agent_turn` |
| Fallback / gap rate | `gap` events ÷ `agent_turn` events |
| Funnel | `conversation_start` → `menu_choice` or first `user_msg` with route ≠ menu → feature started (quiz / agent / slides) → feature completed → close by user (`user_keyword` / `user_intent` / `user_intent_late`) |
| Drop-off | closes with reason `idle`, grouped by `last_step`, with `last_bot_msg_read` true / false / unknown. An idle close after the user's own goodbye or bare thanks is `user_intent_late` instead: a close, not a gap |
| Close reasons | count and share per reason |
| Topics | `user_msg` by topic: messages, unique conversations, share; ES/EN split |
| Gaps board | `gap` by topic and source, with up to 3 most recent scrubbed samples per topic |
| Tools / slides board | counts of tool names from `agent_turn`, slide numbers from `slides_sent` |
| Cost | from `llm_calls` × pricing: total, by model, by purpose, by hour/day, per conversation (mean, p50, p90), per completed quiz, per agent turn |
| Cache hit ratio | cache_read ÷ (input + cache_read + cache_write) |
| Tokens per agent turn | mean / p90 of input and output |
| Reply length | words of each agent reply sent (`agent_turn.reply_words`): mean and p90; trim rate = share cut at `REPLY_MAX_WORDS_HARD` (`agent_turn.trimmed`) |
| Latency | `agent_turn.latency_ms` p50 / p95 |
| Errors | `error` events by kind; `msg_status=failed` by error code (error rate = errors ÷ user messages) |
| Leads | `quiz_complete` count and `handoff` with reason `user_asked` |
| CSAT | if `CSAT_ENABLED`: 👍 ÷ (👍 + 👎) and response rate |
| Spend check | if the Admin key is set: our computed cost vs Anthropic's reported usage for the same UTC days, and the difference as a percentage; flag over 10% |

`anomalies(range)` flags, each with the numbers behind it, when:

- an hour's cost is over 3× the trailing 7-day hourly median (needs ≥24 active hours of history);
- the cache hit ratio is under 50% on a day with more than 20 agent turns;
- guard calls ÷ agent turns is under 0.5 while `GUARD_ENABLED` is on (≥10 turns);
- the error rate is over 5% (≥20 messages);
- the p90 of reply words is over 1.5 × `REPLY_MAX_WORDS` on a day with more than 20 agent turns;
- today's token budget is over 80%.

`GET /admin/metrics?range=7d` (header `X-Admin-Token`) returns the overview, as a debugging aid.

### Daily Slack digest

At `METRICS_DIGEST_HOUR` (default 8, Mexico City), the lifecycle loop posts yesterday's summary
with `chat.postMessage`, top-level, to `SLACK_METRICS_CHANNEL_ID` (fallback: `SLACK_CHANNEL_ID`).
It is short and in Spanish: conversations, containment, escalations by reason, quiz completion,
top 5 topics, top drop-off step, top 3 gaps (names only), cost and anomalies.

- It mentions `SLACK_ALERT_USER_ID` only when there are anomalies.
- A day with no conversations gets a one-liner.
- A `digest:<day>` flag in SQLite prevents a double post after a restart.
- `POST /admin/digest?range=yesterday` sends one now.

### MCP connector for claude.ai (read-only)

Off by default (`MCP_ENABLED=false`). It is served at **`https://origin.alanestrada.space/mcp`**
(Streamable HTTP, official `mcp` SDK). OAuth discovery (`/.well-known/...`) has to live at the
host root, and the root of `www` belongs to the website, so the connector goes on the origin host.
The existing Caddyfile already sends every path on that host to `app:8000`: no deploy change.

**Tools** (all `readOnlyHint: true`; every result states the range and timezone, user ids are
masked to the last 4 digits; 60 calls/min per token; each call is logged by name, duration, row
count and range only):

`get_overview`, `get_funnel`, `get_close_reasons`, `get_topics(range, lang?, top_n)`,
`get_gaps(range, top_n, with_samples)`, `get_tools_and_slides`,
`get_costs(range, group_by=day|hour|model|purpose|conversation)`,
`get_anthropic_usage` (only with `ANTHROPIC_ADMIN_KEY`), `get_conversation_timeline(conversation_id)`,
and `query(sql)`.

`query(sql)` runs one SELECT on a separate `mode=ro` SQLite connection. An authorizer allows only
reads of the views `v_events`, `v_conversations`, `v_llm_calls` (with `cost_usd`), `v_topics` and
`v_gaps`. Base tables, writes, `ATTACH`, `PRAGMA` and non-whitelisted functions are all refused.
Results are capped at 500 rows and 2 seconds. Tool descriptions warn that sample texts are untrusted.

**Auth model** (OAuth 2.1, single owner, built on the SDK's auth helpers):

- **Static client.** `MCP_OAUTH_CLIENT_ID` / `MCP_OAUTH_CLIENT_SECRET` are pasted in claude.ai's
  Advanced settings. Dynamic Client Registration is off, so the static client is required. Both
  `client_secret_post` and `client_secret_basic` work, compared in constant time.
- **`/authorize`** requires **PKCE S256**, and `redirect_uri` must be in `MCP_REDIRECT_URIS`
  (default `https://claude.ai/api/mcp/auth_callback`; check it against the value claude.ai shows).
- **`/mcp/login`** asks for `MCP_OWNER_PASSWORD` (constant-time compare). 5 failures from one IP
  lock that IP for 10 minutes; 20 failures in total lock everyone.
- **Codes** are single-use with a 60-second TTL.
- **`/token`** issues opaque tokens stored as SHA-256 hashes only: 1-hour access tokens and
  30-day rotating refresh tokens.
- **`/revoke`** revokes a token and its refresh family.
- **No or invalid bearer token** → 401 with `WWW-Authenticate: Bearer ..., resource_metadata=".../.well-known/oauth-protected-resource"`.
- **Startup** refuses to enable MCP unless the password has 16+ characters and the client secret,
  client id and `MCP_PUBLIC_URL` are set (it logs why; the bot keeps running).
- **Never logged:** tokens, codes and the password.

**Rotate the password:** change `MCP_OWNER_PASSWORD` in `.env` and redeploy. Existing tokens keep
working until they expire, so to cut access now:
`curl -X POST $URL/admin/mcp/revoke-all -H "X-Admin-Token: $ADMIN_TOKEN"`.
claude.ai then asks you to reconnect. Rotating the client secret also needs the new value in
claude.ai's connector settings.

**Privacy rules:**

- Phone numbers stay inside SQLite; everything that leaves it (metrics, digest, MCP) masks them
  to `****1234`.
- `events` never holds message text.
- Samples are scrubbed and truncated.
- Retention follows `RETENTION_DAYS`.

## Leads

Turns talk attendees into a follow-up list: **who** they are (asked progressively, never
gating anything), **whether they agreed** to be contacted (provable consent, `BAJA` opt-out),
**how warm** they are (explainable score → hot / warm / cold) and **what they care about**
(quiz result, weakest domains, topics, a short need note). Code in `app/leads/`.
`LEADS_ENABLED=false` behaves exactly as before.

Dexter's SQLite is the **system of record**. Zoho CRM (Free) is Alan's sales workspace
(one-way push + daily status pull). Hot leads reach Alan in Slack within a minute, and the
owner dashboard (`/dashboard`) lets him write to them from his **personal** WhatsApp.

### Data model (migration 3 in `app/db.py`)

| Table | What |
|---|---|
| `leads` | one row per person: identity (`user_id` = normalised phone, nullable for email-only web leads; `name`, `company`, `role`, `role_level`, `company_size`, `industry`, `email`, `lang`, `source` `organic`/`web_form`), engagement (`first_seen`, `last_seen`, `last_inbound_at`, `sessions`, quiz level / per-domain scores / weakest domains, `topics_json`, `interest_note`), consent (`consent_followup`, `consent_ts`, `consent_text_version`, asks, `consent_declined_at`, `opted_out_at`), scoring (`fit_score`, `intent_score`, `score`, `tier`, `tier_override`, `contactable`), pipeline (`stage`, `stage_source`, `hot_alerted_at`, `sla_reminded_at`, Slack alert ts), Zoho (`zoho_id`, `zoho_synced_at`, `zoho_dirty`) |
| `lead_activity` | the audit trail: every point earned or lost, consent answers, stage moves, notes, alerts, templates. "Why is this lead hot?" = these rows |
| `contact_requests` | "Que Alan me contacte" (WhatsApp or web): channel, best time, topic, note, `open`/`done`/`cancelled` |
| `zoho_outbox` | pending Zoho upserts / notes / deletes with attempts and backoff |
| `lead_suppressions` | opt-outs (`wa:<phone>`, `email:<address>`): survive retention and erase |
| `dashboard_sessions` | dashboard logins (token stored as a SHA-256 hash) + CSRF token |

A lead row is created on the first **meaningful engagement**: quiz start, first question to the
agent, asking for Alan, a file sent by Dexter, the on-request "making of", or the contact form.
Never on a bare greeting or a menu view. Closing a conversation wipes the chat (as before) but
never the lead.

### Consent and opt-out

The consent question (WhatsApp reply buttons `Sí, escríbeme` / `No, gracias`; numbered text on
Twilio) lives in one place, `app/leads/flows.py` (`CONSENT_TEXT`, `CONSENT_TEXT_VERSION`; the
version is stored with every answer). It covers Dexter **and Alan from his own WhatsApp**, and
links the privacy notice.

- **When:** after the quiz result + report (and the optional profiling questions), after the
  agent records a buying signal, or when the user asks for Alan (before the handoff message).
  At most once per conversation; never during the quiz questions, a form, or human mode.
- **Rules:** "no" → not asked again for 30 days; max 2 asks per lead without a yes; a free-text
  "sí / yes / claro" right after the question counts; anything else is no answer (not re-asked in
  that conversation). Asking for Alan is consent for *that* conversation only.
- **Opt-out:** `baja`, `stop`, `unsubscribe`, `no me escriban`, `no me contacten`, `darme de baja`
  (whole message) — zero tokens, before the guard, in any mode (bot, quiz, human). Records
  `opted_out_at`, stage `opted_out`, suppression record, Zoho update, a Slack thread note (in
  human mode with a warning for Alan), and one confirmation. A later explicit yes (button or
  contact form) lifts it.

### Progressive profiling (never gates value)

After the quiz result and report (quiz scoring unchanged): role level and company size as list
messages, each with `Omitir`, then "¿Pongo tu nombre y empresa en el reporte?". A name/company
answer is split deterministically (`Ana López, Acme`) or, without a separator, with one small
Haiku call (recorded in `usage`); the report is resent with "Preparado para: Ana López, Acme".
Skipped → the report stays as today. Typing anything else ends the questions and is answered
normally. In conversation, the agent fills `update_lead_profile` silently when people volunteer
facts (never interrogates; sensitive data never stored; `interest_note` is scrubbed of phones,
emails, URLs and long digit runs).

### Scoring (`app/leads/scoring.py`, override with `LEAD_SCORING_JSON`)

| Fit (0–40) | Points | | Intent (0–60, capped) | Points |
|---|---|---|---|---|
| owner / general management | 20 | | asked for Alan about their own company | 25 |
| area director | 15 | | buying signal (agent tool) | 20 each, max 2 |
| manager | 8 | | quiz completed | 10 |
| 11–50 or 51–250 people | 15 | | quiz L0/L1 (clear need) | 5 |
| 251+ | 10 | | personalised report requested | 5 |
| 1–10 | 5 | | returning visit (new conversation, another day) | 10 (max 3) |
| company or name known | 5 | | topic PDF / slides | 3 each, max 6 |
| | | | on-request "making of" | 3 |

Intent older than 30 days counts 50%, older than 60 days 0 (recomputed on every change and in
the background loop). **Tier:** hot ≥ 60, warm 30–59, cold < 30; an explicit ask for a meeting or
for help implementing, or a confirmed contact request, makes it hot. Every lead gets a tier; only
`contactable` (consent and no opt-out) leads are actionable.

`flag_buying_signal(kind, note)` — `wants_help_implementing | asked_services_or_price | asked_meeting |
timeline_or_budget_mentioned | team_or_process_pain` — is invisible and never ends the turn; the
prompt forbids pitching, unprompted budget questions and any mention of scoring.

**Stages:** `new → engaged → mql → sql → contacted → meeting → proposal → won | lost | nurture |
opted_out`. Dexter moves leads up to `mql` (warm + contactable) and `sql` (hot + contactable) and
never moves a lead back or over a stage a person set. `contacted` and later come from Slack, the
dashboard, the admin API or the Zoho pull.

### Slack (speed to lead)

- **Hot alert** (once, top-level in `SLACK_LEADS_CHANNEL_ID`, fallback `SLACK_CHANNEL_ID`, mentioning
  `SLACK_ALERT_USER_ID`): name / company / role / size, score with the top 3 reasons, quiz level +
  two weakest domains, interest note, WhatsApp window (time left or "template needed"), links to
  the conversation thread and the dashboard. Contact requests are tagged `📇 Solicitud de contacto`.
- **Commands**, in the conversation thread or the alert thread (never forwarded to WhatsApp):
  `contacted`, `meeting`, `proposal`, `won`, `lost`, `nurture`, `note <text>`, `followup` (then
  `✅ confirm` within 10 minutes).
- **Reminders:** hot + contactable + still `sql` after `LEAD_SLA_HOURS` (24) → one mention with the
  dashboard link; one more 2 h before the 24 h window closes, if it's still open.
- **Digest** at `METRICS_DIGEST_HOUR` (8, Mexico City), skipped on empty days: new leads by tier,
  consent answers, hot leads past the SLA, opt-outs, stage moves.
- Without the Slack app (webhook only) alerts and digest go to the webhook; commands need the app.

### Re-engagement within WhatsApp policy

Inside the 24 h window Alan replies from Slack as today. Outside it: only the approved follow-up
template (`TEMPLATE_FOLLOWUP_ES` / `_EN`, by `lead.lang`), only to contactable leads, one lead at a
time (Slack `followup` + confirm, dashboard button, `POST /admin/leads/{id}/followup`), at most one
every 7 days per lead. Drafts to submit in WhatsApp Manager: `deploy/leads/whatsapp-templates.md`.

### Owner dashboard (`https://origin.alanestrada.space/dashboard`)

Server-rendered, phone-first, light/dark. `DASHBOARD_ENABLED=true` + `DASHBOARD_PASSWORD` (16+
characters, constant-time compare, 5 failures → 15 min lockout per IP), session cookie
`HttpOnly; Secure; SameSite=Strict` (12 h, stored hashed), CSRF token on every POST, `noindex`.

- **Contact requests** (oldest first) · **Hot, not reached out** (contactable, hot, `sql`, no
  message from them in 48 h) · **All leads** (tier, stage, consent, source, date, search).
- **Lead page:** profile, score breakdown, quiz, topics, interest note, consent record, history,
  Slack and Zoho links, window state.
- **Actions (contactable only):** *Escribir desde mi WhatsApp* (`wa.me` link with a draft in the
  lead's language from `app/leads/drafts.py`; Alan edits and sends — Dexter never sees it, so…),
  *Marcar contactado* with channel + note (stops the SLA timers, closes open requests, syncs
  Zoho), stage buttons, notes, Dexter template button when the window is closed. `tel:` / `mailto:`
  links. Non-contactable leads show a masked phone and no contact buttons.
- `GET /dashboard/leads.csv`: contactable leads, unmasked. Logins and actions are logged (never
  the password or cookie).

### "Que Alan me contacte"

- **WhatsApp:** menu option 4 now offers *Hablar con Alan ahora* (the existing handoff) or *Que Alan
  me contacte* (typed `hablar con alan` / `humano` still hand off directly). Also the keywords
  `contáctenme`, `que me contacten`, `que me llame`, `call me`, `contact me`, and the agent's
  `request_contact` tool. Steps: channel (WhatsApp / Llamada / Correo) → best time → topic →
  name and company → email (if Correo; one retry, then WhatsApp) → `Confirmar` / `Corregir`.
  Confirming is the explicit consent (version `wa-form-…`): request row, hot, Slack alert, Zoho
  note. No handoff. Leaving mid-way saves nothing. Not offered in human mode.
- **Web:** `POST /contact` (via `https://www.alanestrada.space/whatsapp-bot/api/contact`, same
  origin): name, company, role, size, email and/or WhatsApp, channel, message, consent checkbox
  (required, unchecked by default). Honeypot, 3 s minimum fill time, 5/h per IP, 8 KB max,
  generic errors. A phone merges with the WhatsApp lead; otherwise email dedupes. The page to
  copy into the `space` repo: `deploy/web/contacto.html`.

### Zoho CRM (Free edition)

All logic stays in Dexter; Zoho stores and displays. `app/leads/zoho.py`:

- OAuth Self Client (`ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`; scopes
  `ZohoCRM.modules.leads.ALL`, `ZohoCRM.modules.notes.CREATE`, `ZohoCRM.settings.fields.READ`,
  `ZohoCRM.coql.READ`), `ZOHO_ACCOUNTS_URL` / `ZOHO_API_DOMAIN` of the org's data center, REST v8.
  Access token cached in memory, refreshed on 401, never logged.
- Pushes **Leads** on consent, tier, stage, profile changes and opt-out (`ZOHO_SYNC_SCOPE=contactable`
  or `all_engaged`), debounced 60 s per lead, 2 calls at a time, exponential backoff, and stops for
  the day at 80% of `ZOHO_DAILY_CREDIT_BUDGET` (one Slack alert). First sync looks the record up by
  `Mobile` (COQL; the stock layout doesn't make `Mobile` unique), later ones update by id.
- Mapping: `Last_Name` (→ WhatsApp name → `WhatsApp ••1234`), `First_Name`, `Company` (`—` if
  unknown), `Mobile`, `Email`, `Designation`, `No_of_Employees` (bucket midpoint), `Lead_Source`
  (`ZOHO_LEAD_SOURCE_WHATSAPP` / `_WEB`, `Chat` today), `Lead_Status` (below), `Description`
  (tier/score + reasons, quiz, industry, interest, consent), `Email_Opt_Out`, tags `dexter` + tier
  (+ `meeting` / `proposal` / `won` / `opted_out`). Custom fields used when they exist:
  `Dexter_Score` (number), `Dexter_Tier` (single line), `Dexter_Quiz_Level` (number),
  `Dexter_Consent_At` (date/time), `Dexter_Lead_ID` (single line). Missing ones are logged at startup.
- `Lead_Status`: new/engaged → Not Contacted · mql/sql → Pre-Qualified · contacted/meeting/proposal/won
  → Contacted · nurture → Contact in Future · lost → Lost Lead · opted_out → Not Qualified.
- Hot alert → one Zoho note (the Slack summary, no transcript). Daily COQL pull: Contacted →
  `contacted` (unless Dexter already has a later stage), Lost Lead / Junk Lead / Not Qualified →
  `lost`, Contact in Future → `nurture`; Zoho wins for stages ≥ `contacted`.
- Erase deletes the Zoho record. Backfill: `docker compose exec app python -m app.leads.zoho backfill --dry-run`
  (phones masked), then without `--dry-run`. `ZOHO_ENABLED=false` (default): no calls at all.

### Read access ("chat with Claude about my leads")

Read access is via `/admin` (X-Admin-Token). Lead tools on the metrics MCP connector
(`get_leads`, `get_lead`, `get_lead_funnel`) are not wired yet:

```bash
curl -s "$URL/admin/leads?tier=hot&contactable_only=true" -H "X-Admin-Token: $ADMIN_TOKEN"   # phones masked
curl -s "$URL/admin/leads.csv?unmask=1" -H "X-Admin-Token: $ADMIN_TOKEN" > leads.csv
curl -s "$URL/admin/leads/funnel?days=30" -H "X-Admin-Token: $ADMIN_TOKEN"
# engaged, consented, warm, hot, contacted, meeting, won, consent_rate, opt_out_rate,
# median_hours_to_contact_hot, sla_breaches
```

For Claude: connect the Zoho CRM connector in claude.ai and add the skill in
`deploy/claude/dexter-leads/SKILL.md` (field names, tags and statuses Dexter writes).

### Retention and ARCO

- Leads without consent: purged with `RETENTION_DAYS` (30) from `last_seen`, like conversations.
- Consented leads: `LEAD_RETENTION_DAYS` (365) from `last_seen`.
- Opt-out: consent is withdrawn at once; the lead then follows the no-consent retention, and a
  suppression record (number/email + date) is kept so Dexter never asks again.
- `POST /admin/leads/{lead_id}/erase` (X-Admin-Token): deletes the lead, its activity, outbox rows,
  contact requests and the Zoho record; keeps the suppression record only if the person opted out.
- Privacy notice draft (LFPDPPP 2025): `deploy/leads/aviso-privacidad-leads.md` — needs a legal review.

## Evals

A repeatable set of cases (`evals/cases/*.json`, JSON so there is no new dependency) to check a
prompt or model change before deploying. Each case runs through the real webhook path:

```json
{"id": "close-seria-todo", "lang": "es", "prior": ["hola"], "message": "Sería todo, gracias",
 "expect": {"route": "close", "must_contain": ["Cerré la conversación"], "language": "es",
            "state": {"closed": true}}}
```

- `route`: `menu | quiz | agent | close | handoff | offtopic_warning | file_reply | on_request | consent | opt_out | lead_form` (a list means "any of"). `file_reply` = the bot answered with a file (slides, report); `file_note` = a static reply about a file the user sent.
- `must_contain` / `must_not_contain`: case-insensitive substrings of the reply; `language`: `es` / `en`.
- `tool_called` / `tool_not_called` (e.g. `end_conversation`, `start_assessment`, `send_slides`, `talk_to_alan`, `flag_buying_signal`); `state`: `quiz_active`, `handoff`, `closed`, `file_sent`, and for leads `consent`, `consent_asked`, `opted_out`.
- `env`: settings for that case only, e.g. `{"LEADS_ENABLED": "true"}` (the suite runs with leads off by default; `evals/cases/10_leads.json` turns them on).
- `media`: an inbound photo/PDF (`{"type": "image", "caption": "..."}`).
- `topic`: the topic label the message gets in the metrics (`user_msg` event), e.g. `roles_ownership` (see "Metrics & governance"); `evals/cases/10_topics.json` covers ~13 representative messages.
- `mock` (offline only): the guard verdict (`"guard": "OUT"`, `"prior_guard"`) and the agent's scripted steps (`[{"tool": "send_slides", "input": {"slide": "8"}}, {"text": "Va."}]`).
- `max_words`: the turn's replies together stay within N words (the agent cases carry 150). `mock.prior_agent` (offline only): the texts the agent gives to the earlier turns, e.g. one that ends with "¿Algo más…?".

**Offline** (default, part of `pytest`): the Anthropic API is mocked with respx, so it checks
routing, tool dispatch, state and the deterministic replies; text checks are skipped for
model-written replies. No network.

**Live** (real models, never part of the normal suite):

```bash
set -a; source .env; set +a                     # or export ANTHROPIC_API_KEY yourself
RUN_LIVE_EVALS=1 pytest -m eval_live -s         # every expectation checked, table printed
EVAL_CASES=talk-es-10-20-70,inject-en-dan RUN_LIVE_EVALS=1 pytest -m eval_live -s   # a subset
```

It prints `case / PASS|FAIL / reason` and writes `evals/results/<timestamp>.json` (git-ignored).

**Add a case**: append an object to the matching file in `evals/cases/` (ids must be unique), or
turn a real conversation into a draft:

```bash
python scripts/transcript_to_case.py --slack thread-export.json --thread 1700000000.000100
python scripts/transcript_to_case.py --db data/bot.sqlite3 --user 523312345678 --turn 3 \
    --out evals/cases/90_regressions.json
```

The draft keeps the bot's real reply under `observed`; anonymize it and fill in `expect`.
