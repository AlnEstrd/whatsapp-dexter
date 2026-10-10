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
(not sent). Thread links (and who has control) are saved to `.slack_threads.json`, so
restarts and `--reload` keep existing threads working; the agent's own
memory is not saved. A command typed in a thread that is no longer linked
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

- **Persistence**: swap `app/store.py`'s in-memory dict for Redis/Postgres.
- **More tools**: add real business logic in `app/tools.py` following the
  `get_current_datetime` / `calculate` pattern (register in
  `TOOL_DEFINITIONS`, implement, wire into `run_tool`).
- **Media messages**: `parse_webhook_payload` currently only extracts text;
  extend it for images/audio/documents if you need those.
- **Production**: move off ngrok to a real host with a stable HTTPS
  endpoint, add signature verification on `/webhook`
  (`X-Hub-Signature-256`), rate limiting, and structured logging/observability.
- **Migrate to the real API**: everything here is the *same* Cloud API
  you'd use in production — moving off the "test number" mainly means
  verifying a business and requesting a real phone number.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

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
  agent.py             Claude tool-use loop + EchoAgent (AGENT_MODE=echo)
  escalation.py        Slack escalation + provider event alerts (Incoming Webhook)
  slack_bridge.py      Slack app bridge: thread per conversation, take/bot, /slack/events
  handoff.py           Handoff router: Flex (Conversations + Interactions API) or Slack
  twilio_api.py        Minimal Twilio REST helper
  resilience.py        Retry with exponential backoff
  tools.py             Tool definitions + implementations
  docs/                PDF generation (reportlab): slides.py, topic.py, assessment.py, common/theme
  store.py             In-memory history, handoff state, webhook dedup, Slack thread map
  config.py            Settings loaded from .env
tests/                 pytest suite (160 tests; no network, HTTP mocked with respx)
```

## Talk companion flows ("Gobernanza de IA")

With `FLOWS_ENABLED=true` (default) the bot is the companion for Alan's talk:

| Flow | Trigger | How it works |
|---|---|---|
| Welcome menu | greeting (`hola`, `menu`, …), or after the first question | WhatsApp list message on Meta; numbered text elsewhere (`1`–`5`) |
| Explore the talk | menu 1 or any question | Claude answers from `app/kb/core/*.md` (whole, prompt-cached) and searches `app/kb/sources/*` with the `search_sources` tool (BM25, no vector DB) |
| AI maturity assessment | menu 2, `diagnóstico`/`quiz`, or the agent's `start_assessment` tool | Deterministic (`app/flows/quiz.py`): 9 questions on the talk's 7 domains + adoption + measurement, L0–L3, cap at L1 without sponsorship/ownership, 3 next steps. Result note goes to the Slack thread. `salir`/`exit` stops it |
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

## Files from attendees (photos, screenshots, PDFs)

Attendees can send a JPG/PNG/WebP/GIF or a PDF (with or without a caption) and ask about it. The bot downloads it
(Graph `GET /{media_id}`, then the short-lived URL with the Bearer token), validates it and shows it to Claude for **that turn only**.

- Limits: photos 4 MB, PDFs 8 MB and 20 pages (`app/attachments.py`). The size is checked on the declared size and while streaming; the real type is sniffed from the bytes, not trusted from the sender.
- Nothing is saved to disk, and the chat history keeps only a text note (`[Attached image: name]`), never the file.
- The prompt tells Claude to treat file contents as data, not instructions, and not to repeat IDs or card numbers it sees.
- Audio, video, stickers, other types, files too big, unreadable PDFs and download failures get a short polite reply (ES/EN). During the quiz, a file is asked to wait.
- A caption like `humano` still hands off to Alan. In the Slack thread the file shows as `📎 name`; the file itself is not forwarded.
- Meta only. Twilio gets the "photos, PDFs and text only" reply.

Try it without WhatsApp:

```python
from app import docs
open("deck.pdf", "wb").write(docs.make_deck().data)            # whole deck
open("s8.pdf", "wb").write(docs.make_deck("8").data)           # one slide
```

## Conversation lifecycle & guardrails

| Situation | What the user gets | Config |
|---|---|---|
| User says goodbye (`adiós`, `bye`, `terminar`, `salir`…; inside the quiz `salir` only exits the quiz) | Closing message; memory wiped; next message starts fresh with the menu | — |
| No activity | "¿Sigues ahí?" warning, then a closing message | `IDLE_WARN_MINUTES=10`, `IDLE_CLOSE_MINUTES=5` (chats with a human in charge are skipped) |
| Alan closes from Slack (`close` or 🔒 on the parent) or `POST /admin/close` | "Alan cerró esta conversación…" | — |
| Server restart wiped an active chat (agent history or a half-done quiz) | Apology from Alan saying the history was lost — sent on startup to chats active in the last 23 h, or before the reply to their next message | `RESTART_NOTICE=true`, `SESSIONS_FILE` |
| Message off the talk's topic | A cheap classifier (`GUARD_MODEL`, Haiku) runs first; off-topic gets a static warning "Aviso 1 de 2 / 2 de 2" with no main-agent call; the next one hands the chat to Alan | `GUARD_ENABLED`, `OFFTOPIC_MAX_WARNINGS=2` |

The session registry (`data/sessions.json`) and the Slack thread map live on the `app_data` Docker volume, so they survive redeploys. Messages of ≤ 3 words skip the classifier (follow-ups like "¿y en ventas?"). The guard fails open: if the classifier errors, the main agent answers.
