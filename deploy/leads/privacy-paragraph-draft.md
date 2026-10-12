# Privacy notice paragraph: draft for `privacy/index.html`

**Draft only, for the planned legal review. Not published. Do not add it to `AlnEstrd/space` until it is approved.**

## Español

**Seguimiento después de la plática.** Si conversas con Dexter, el asistente de Estrada AI Lab, Alan Estrada
puede escribirte una sola vez, de forma personal, después de la plática, para ver cómo te fue y si puede
ayudarte. Si prefieres que no, responde *No, gracias* o escribe *BAJA* en cualquier momento, o escríbenos a
me@alanestrada.space. Si nos das tu correo electrónico, lo usamos para enviarte recursos de la plática y avisarte
de futuras pláticas. Conservamos los datos de quien dijo que sí, o no objetó el aviso, hasta 365 días después de su última
actividad; quien dijo que no se borra a los 30 días. Quien se da de baja conserva solo su número y la
fecha de baja, para no volverle a escribir. **[Legal: confirmar si los datos de contacto se sincronizan con el CRM
(Zoho) y, de ser así, nombrar al proveedor y su país.]**

## English

**Follow-up after the talk.** If you chat with Dexter, the assistant of Estrada AI Lab, Alan Estrada may write to
you once, personally, after the talk, to see how it went and whether he can help. If you'd rather he didn't, reply
*No, thanks* or type *STOP* at any time, or write to us at me@alanestrada.space. If you give us your email address,
we use it to send you resources from the talk and to tell you about future talks. We keep the data of people who
said yes, or did not object to the notice, for up to 365 days after their last activity; people who said no are
deleted after 30 days. Someone who unsubscribes keeps only their number and the date of unsubscription, so that we
never write to them again. **[Legal: confirm whether contact data is synced to the CRM (Zoho) and, if so, name the
provider and where it is based.]**

---

### Notes for the reviewer (not for publication)

- The paragraph matches what the code does: one personal follow-up (`tacit_notice` or `explicit`), email only for
  resources and future talks, BAJA / STOP and `me@alanestrada.space` as the ways out, and the retention in the
  README ("Consent & follow-up").
- Dexter never sends email; Alan writes from his own mailbox.
- Zoho sync is built in (`ZOHO_ENABLED`, off by default). Whether it is switched on decides the bracketed sentence.
- Retention follows the code: `explicit` and `tacit_notice` (the welcome shown, no "no") keep
  `LEAD_RETENTION_DAYS` (365) from the last activity; `declined`, and leads with no consent, keep `RETENTION_DAYS`
  (30). Opt-outs keep only their suppression record.
