# Leads — checklist for Alan and phone test script

## Checklist (in order)

1. **Slack:** create `#dexter-leads`, `/invite` the Dexter app, copy its channel ID (channel →
   About, bottom) into `SLACK_LEADS_CHANNEL_ID`.
2. **Templates:** submit the two templates in `deploy/leads/whatsapp-templates.md` in WhatsApp
   Manager (Marketing). When approved, set `TEMPLATE_FOLLOWUP_ES` / `TEMPLATE_FOLLOWUP_EN`.
3. **Website (`AlnEstrd/space`):** review `deploy/leads/aviso-privacidad-leads.md` (get a legal
   review), publish it in `privacy/index.html`, and copy `deploy/web/contacto.html` to
   `contacto/index.html` (adjust colors/fonts to the privacy page if needed).
4. **Deploy** with `LEADS_ENABLED=true`, `DASHBOARD_ENABLED=true`, a `DASHBOARD_PASSWORD` of 16+
   characters (e.g. `openssl rand -base64 24`), `ZOHO_ENABLED=false`. Open
   `https://origin.alanestrada.space/dashboard` on the iPhone, log in, Share → *Add to Home Screen*.
5. **After the talk — Zoho:**
   1. sign up for Zoho CRM Free with the alanestrada.space Zoho account (same data center as the
      existing org);
   2. Setup → Modules → Leads → Fields: create `Dexter_Score` (Number), `Dexter_Tier` (Single
      Line), `Dexter_Quiz_Level` (Number), `Dexter_Consent_At` (Date/Time), `Dexter_Lead_ID`
      (Single Line); in **Lead Source** add `WhatsApp – Dexter` and `Web – Dexter`;
   3. api-console.zoho.com → *Self Client* → generate a code with scope
      `ZohoCRM.modules.leads.ALL,ZohoCRM.modules.notes.CREATE,ZohoCRM.settings.fields.READ,ZohoCRM.coql.READ`,
      exchange it for a refresh token, paste `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`,
      `ZOHO_REFRESH_TOKEN` (and `ZOHO_ACCOUNTS_URL` / `ZOHO_API_DOMAIN` if not `.com`) in `.env`;
      set `ZOHO_LEAD_SOURCE_WHATSAPP=WhatsApp – Dexter`, `ZOHO_LEAD_SOURCE_WEB=Web – Dexter`;
   4. `docker compose exec app python -m app.leads.zoho backfill --dry-run`, check the output, then
      `docker compose exec app python -m app.leads.zoho backfill`;
   5. set `ZOHO_ENABLED=true` and redeploy (the startup log lists any missing custom field).
6. **Claude:** connect the Zoho CRM connector in claude.ai and install the `dexter-leads` skill
   (`deploy/claude/dexter-leads/SKILL.md`).

## Phone test script

1. From your phone: *hola* → 2 (diagnóstico) → answer the 9 questions.
2. After the result + PDF: pick a role and a size, then type `Tu Nombre, Tu Empresa` → the report
   comes back with "Preparado para: Tu Nombre, Tu Empresa".
3. Tap **Sí, escríbeme** → "¡Gracias! Anotado…".
4. Ask: *¿Alan me puede ayudar a implementar esto en mi empresa?* → within a minute a 🔥 alert in
   `#dexter-leads` with your score, reasons, quiz level and the window state.
5. Open the dashboard → *Calientes sin contactar* (or the lead page) → **Escribir desde mi
   WhatsApp**: your own WhatsApp opens with the draft → don't send, go back → **Marcar
   contactado** (Mi WhatsApp) → the lead leaves the hot list; the Slack alert thread accepts
   `note …` too.
6. From a second phone: *4* → **Que Alan me contacte** → Llamada → Tarde → skip the topic → name
   → **Confirmar** → a 📇 alert in Slack and the request at the top of the dashboard.
7. Web form (`/contacto`): submit once **without** the consent box (blocked) and once with it →
   📇 alert + dashboard.
8. From the first phone send `BAJA` → "Listo, ya no te escribiremos…", a 🚫 note in its Slack
   thread, the lead shows *BAJA* in the dashboard (and, once Zoho is on, `Not Qualified` +
   `Email_Opt_Out`).
