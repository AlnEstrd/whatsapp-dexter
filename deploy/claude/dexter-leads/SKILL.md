---
name: dexter-leads
description: Work with the leads that Dexter (Alan Estrada's WhatsApp bot for the "Gobernanza de IA" talk) syncs into Zoho CRM. Use when Alan asks about his leads, who to follow up with, a lead's context, or to update a lead's status, notes or tags in Zoho CRM.
---

# Dexter leads in Zoho CRM

Dexter's SQLite is the system of record; Zoho CRM (Free edition, org "Estrada Labs",
America/Mexico_City) is Alan's sales workspace. Dexter pushes leads one way and pulls only
`Lead_Status` back once a day. Use the Zoho CRM connector tools.

## What Dexter writes (module **Leads**)

- Only leads that **consented** to follow-up are synced (scope `contactable`), plus opt-outs that
  were synced before. Find them with the tag `dexter` or `Lead_Source = 'Chat'` (later
  `WhatsApp – Dexter` / `Web – Dexter`).
- Tags: `dexter`, exactly one tier tag `hot` / `warm` / `cold`, plus `meeting`, `proposal`, `won`,
  `opted_out` when they apply.
- `Description` holds the explanation: tier and score (fit + intent) with the top reasons, the
  AI-maturity quiz level (L0–L3) and the two weakest domains, industry, interest note, and the
  consent timestamp + text version.
- Custom fields, if Alan created them: `Dexter_Score`, `Dexter_Tier`, `Dexter_Quiz_Level`,
  `Dexter_Consent_At`, `Dexter_Lead_ID`.
- Notes: "Dexter: lead caliente" (the hot-lead summary, never a transcript) and Alan's notes from
  Slack or the dashboard.
- `Mobile` is `+52…` (E.164). `Email_Opt_Out = true` and `Lead_Status = Not Qualified` + tag
  `opted_out` mean the person typed BAJA: **never suggest contacting them**.

## Status mapping

| Dexter stage | `Lead_Status` |
|---|---|
| new, engaged | Not Contacted |
| mql, sql | Pre-Qualified |
| contacted, meeting, proposal, won | Contacted (+ tag `meeting` / `proposal` / `won`) |
| nurture | Contact in Future |
| lost | Lost Lead |
| opted_out | Not Qualified (+ `Email_Opt_Out`, tag `opted_out`) |

Changes Alan makes in Zoho flow back to Dexter once a day: Contacted → contacted (unless Dexter
already has a later stage), Lost Lead / Junk Lead / Not Qualified → lost, Contact in Future →
nurture. Zoho wins for stages ≥ contacted. Other fields edited in Zoho are **overwritten** by the
next Dexter push — record context as a **Note** instead.

## How to help Alan

- "Who should I call today?": `hot` first, then `warm`, both with `Lead_Status = Pre-Qualified`,
  newest first; show name, company, role, the quiz level and the reasons from `Description`.
- Updating a lead: set `Lead_Status` (per the table) and add a Note; never edit `Description`,
  tags or the `Dexter_*` fields by hand.
- Respect the free plan: prefer one COQL query (`select … from Leads where Tag = 'dexter' …`) over
  many single reads.
- Never send messages to leads from here. Alan writes from his own WhatsApp via the Dexter
  dashboard (`https://origin.alanestrada.space/dashboard`) or sends the approved template.
