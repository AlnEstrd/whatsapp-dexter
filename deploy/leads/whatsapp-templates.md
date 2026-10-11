# Follow-up templates (drafts to submit in WhatsApp Manager)

Alan submits these himself in **WhatsApp Manager → Account tools → Message templates → Create
template**. Dexter never creates templates via the API; it only sends an approved one, to one
contactable lead at a time (Slack `followup` + `✅ confirm`, the dashboard button, or
`POST /admin/leads/{id}/followup`), at most once every 7 days per lead.

After approval, put the exact template names in `.env`:

```
TEMPLATE_FOLLOWUP_ES=dexter_seguimiento_es
TEMPLATE_FOLLOWUP_EN=dexter_followup_en
```

Dexter sends one body variable, `{{1}}` = the person's first name, or a neutral fallback when it
is unknown (`qué tal` → "Hola qué tal,", `there` → "Hi there,"). Language codes used:
`es_MX` and `en`, so create the Spanish one as **Spanish (MEX)** and the English one as **English**.

---

## 1. `dexter_seguimiento_es`

- **Category:** Marketing
- **Language:** Spanish (MEX) — `es_MX`
- **Header:** none (or text: `Gobernanza de IA`)
- **Body:**

> Hola {{1}}, soy Alan Estrada. Gracias por platicar con Dexter después de la plática *Gobernanza de IA*. Te comparto los recursos de la plática y, si te sirve, podemos tener una llamada de 20 minutos para ver cómo aplicarlo en tu empresa. ¿Te late?
>
> Responde BAJA para no recibir más mensajes.

- **Sample for `{{1}}`:** `Ana`
- **Buttons (optional, quick reply):** `Sí, agendemos` · `Mándame los recursos`

## 2. `dexter_followup_en`

- **Category:** Marketing
- **Language:** English — `en`
- **Header:** none (or text: `AI Governance`)
- **Body:**

> Hi {{1}}, this is Alan Estrada. Thanks for chatting with Dexter after the *AI Governance* talk. Here are the talk resources, and if it helps, we can have a 20-minute call to see how to apply it at your company. Interested?
>
> Reply STOP to stop receiving messages.

- **Sample for `{{1}}`:** `Ana`
- **Buttons (optional, quick reply):** `Yes, let's talk` · `Send the resources`

---

Notes

- Quick-reply taps and any reply re-open the 24 h window: Dexter (or Alan from Slack) answers as
  usual. `BAJA` / `STOP` are handled by Dexter with zero model calls and recorded as an opt-out.
- If Meta rejects the Marketing category wording, keep the opt-out line and the single variable;
  Dexter only depends on the template **name**, the language and `{{1}}`.
- Marketing templates are paid per conversation and Meta may limit delivery to users who don't
  engage. Send them only to leads who said yes and haven't answered in more than 24 h.
