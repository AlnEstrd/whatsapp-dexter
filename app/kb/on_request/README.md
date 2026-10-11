# On-request information

Things Dexter knows but **only tells people who explicitly ask**. An entry here is never a
menu option, never suggested, never hinted at, and never in the system prompt, the core KB
(`app/kb/core/`) or the `search_sources` index. If nobody asks, nobody knows it exists.

How it stays hidden: the model can't suggest what it can't see. An entry reaches a user in
two ways only:

1. **Fast path** – the user's message matches one of the entry's `triggers`. The bot sends
   `## Respuesta` as written (no model call, works even when the token budget is spent).
2. **Agent path** – the model calls `lookup_private_info(query)`, a generic tool whose
   description names no topic. The server runs the same matcher on the query.

After that, the entry is *unlocked* for that conversation only: its `## Detalle` joins that
user's agent context (so follow-ups work), follow-ups about it are not off-topic, and `pdf`
sends the whole entry as a PDF (if `pdf: true`). Closing the chat locks it again.

## File format

One Markdown file per entry (`README.md` is ignored):

```markdown
---
id: my-entry              # slug: a-z, 0-9, - or _
enabled: true             # false = ignored
title: Title for the PDF
topic: one line telling the off-topic guard what follow-ups are about
lang: [es, en]
triggers:                 # explicit phrasings that count as asking for it
  - "como se hizo dexter"
  - "how was dexter built"
min_score: 0.75           # optional, 0.3-1.0
notify_slack: true        # silent "🔑 On-request info shared: <id>" note in the user's thread
pdf: true                 # allow "pdf" to send the whole entry as a document
lead: true                # lead magnet: keep inviting the reader to talk to Alan (see below)
---

<!-- comments are stripped and never sent -->

## Respuesta
What the bot sends (Spanish). Keep it under ~1,000 characters; over 4,000 is rejected.

## Response
Optional English version for English speakers.

## Contacto
Optional invitation to talk to Alan (Spanish), sent as its own bubble after the reply and the PDF.

## Contact
Same, in English.

## Detalle
Everything the bot may use for follow-ups. Light Markdown: ### headings, - bullets
(two-space indent for a sub-level), 1. numbered items, *bold*, `code`.
```

Placeholders (rendered at disclosure time, per user, never cached):

- `{{status_line}}` – one sentence: version, deploy date, uptime, passing tests.
- `{{status}}` – short bullet list: the above plus Meta verification state, live/off
  features and public counters. See `app/status.py`.

Unknown placeholders render empty and are logged at startup.

## Lead entries (`lead: true`)

Share generously, and keep the human route one message away:

- the `## Contacto` invitation follows the reply and the PDF;
- the agent repeats it naturally (costs, building one, their company, working with Alan);
  after three answers in a row without it, the bot adds it itself;
- when that person asks for Alan, the reply is personal and the Slack alert starts with
  `🔥 Lead (read: <id>)` so Alan knows what they read.

## Matching

Deterministic, triggers only (the content is never searched):

- text is lowercased, accents/punctuation/emoji stripped;
- a trigger that appears whole in the message scores 1.0;
- otherwise: the share of the trigger's meaningful words found in the message, discounted
  when the message is mostly about something else. Triggers with fewer than two meaningful
  words (e.g. "cómo te hicieron") only match as a whole phrase.

Add more triggers rather than lowering `min_score`.

## Rules

- **No secrets, tokens, IDs, IPs, SSH or admin details in entries.** Anyone who asks gets it.
- Keep `## Respuesta` short; put the depth in `## Detalle`.

## Adding or editing an entry

- Normal path: edit or add a file here → PR → merge to `main` → CI/CD deploys.
- Entries that must not live in git: copy them to the server volume, same format. Repo and
  volume entries merge; on the same `id` the volume wins. Restart the app to reload.

  ```bash
  ssh whatsapp-bot 'mkdir -p /tmp/onreq'
  scp my-entry.md whatsapp-bot:/tmp/onreq/
  ssh whatsapp-bot 'cd /opt/whatsapp-bot/deploy && docker compose exec -T app mkdir -p /srv/data/on_request \
    && docker compose cp /tmp/onreq/my-entry.md app:/srv/data/on_request/ && docker compose restart app'
  ```

Config: `ON_REQUEST_ENABLED` (default true), `ON_REQUEST_DIR` (default this folder),
`ON_REQUEST_EXTRA_DIR` (default `data/on_request`), `META_VERIFICATION_STATUS` (status fallback).
