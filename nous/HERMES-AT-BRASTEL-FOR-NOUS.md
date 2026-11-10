# Hermes at Brastel: agents, automations, and what we want to improve

*Public, summarized version prepared for our conversation with Nous Research on November 10, 2026. People's names, server addresses, and internal details have been removed.*

**How to comment:** open an issue in this repository (title starting with `[Nous]`). Agents are welcome to read and comment too.

---

## 1. How we are organized today

| Agent | Role | Where it runs |
|---|---|---|
| **Nara** | the owner's personal assistant: email, calendar, alerts, daily briefs; orchestrates the area agents | official Hermes, separate server (no direct access to production) |
| **Sofia** (Executive) | resolves blocked requests from the areas; curates the company memory | official Hermes, production |
| **Íris** | the AI's single identity on email and Teams; follows up with managers in our goals tool (Scopi) | official Hermes, production |
| **Finance, Operations, Sales, Quality, HR** | one agent per area, each with only the access it needs | official Hermes, production |
| **Claude Code** | maintenance (fixes, configures, versions); does not serve end users | owner's PC and the lab server |
| **Joe** (Grok) | reads GitHub, consolidates open items, chases deadlines; never touches production | its own VM |

Agents talk to each other over **A2A** (Nara asks the area agents). Blockers become cards on a Kanban board; the Executive decides, and the owner is called only when the decision is his.

## 2. Automations (39 in total, summary)

✅ works · ⚠️ partial · ❌ broken or missing

| Group | Automations | Status |
|---|---|---|
| Owner's routines (Nara) | morning brief, evening brief, urgent-email alert, inbox triage with 3 briefs a day, calendar alarm, single to-do list (Google Tasks), resume a question cut off by a restart | ✅ (triage is new as of Nov 9) |
| Finance | ERP access (Senior, read-only), payment-batch review, next-day bank reconciliation, payment audit, corporate card | ✅ for one group company; ⚠️ the other has no bank-statement API; ⚠️ card API quota was fixed on Nov 9 |
| Management | daily follow-up with managers in Scopi, done by Íris | ✅ |
| Owner's WhatsApp | "Mirror": listen-only; transcribes voice notes (local Whisper) and reads screenshots (local OCR) to give Nara context | ✅ moved to the official Hermes WhatsApp bridge on Nov 9 |
| Areas | Operations (fleet, purchasing), Sales (quotes, tenders), Quality, HR | ✅ |
| Monitoring | "Sentinel" (non-AI watchdogs, phone alerts, board cards), hourly maintenance rounds | ✅ but nothing watches the Sentinel itself |
| Memory | company brain (GBrain on Postgres) + graph (Hindsight) | ✅ new on Nov 9 |
| Still missing | querying our internal process wiki; detecting when the owner accepts a meeting on WhatsApp | ❌ |

## 3. Our migration strategy, and what we lost along the way

**Where we came from:** a single agent on a **fork** of Hermes, with broad server access, its own GitHub tools, and permission to change its own configuration. It **configured itself, fixed its own problems, and published to GitHub on its own**. That came at a cost:
- a corrupted conversation database;
- conflicting locks;
- the fork broke on updates;
- the owner's personal matters and company matters lived in the same place.

**Where we are going:** **official Hermes, one area at a time**, and then shut down the fork.
- One agent per area, with least-privilege access.
- The owner's agent on a separate server.
- Nothing outside official Hermes.

**Why:** security, isolation (a failure in one area doesn't take down the others), and getting official updates without patching a fork.

**The side effect:** our agents became **safer but less autonomous**. Capabilities of the old agent did not carry over. Two cases from the same week:
- Nara couldn't read a whole GitHub issue (comments come in pages of 30) or post with her own token;
- Nara said email "didn't support attachments" when the integration does: what was missing was the function and a skill teaching her to use it.

We also learned that maintenance (Claude) sometimes **fixed things for the agent instead of teaching it**, so the capability never stayed with the agent.

**Next step:** a **parity test**. We list what the old agent did on its own, test it on the new agents, and turn every failure into a tool plus a skill that the agent itself owns.

## 4. The errors we see: model or design?

Most recent errors came from what reaches the model, not from the model itself:
- **A tool the agent didn't know about** (the attachment case).
- **Incomplete context:** an alert asked the owner for a "decision" that had already been made.
- **Old rules stuck in an open conversation:** the session keeps the old SOUL until `sessions repair-prompts` runs.
- **Conflicting instructions:** a skill pointed to a knowledge source that was months out of date.

## 5. AI models in use

| Who | Model |
|---|---|
| All official agents and Nara | `gpt-6.1-terra`, `high` reasoning, through a ChatGPT subscription (`openai-codex` provider) |
| Legacy fork (being retired) | `gpt-6.1-sol` |
| Fact extraction for the graph (Hindsight) | `gemini-2.5-flash-lite` via OpenRouter (< US$ 2/month) |
| Reading receipts and technical drawings | Gemini (free tier) |
| Audio | local Whisper `base` |
| Maintenance | Claude Code |

## 6. Memory: what we tried

- **Graphiti:** extraction kept failing (it needs a strong model), and it was almost never queried. Retired.
- **Honcho:** injected context into every message and barely improved answers. Replaced.
- **GBrain on PGLite:** only allowed one process and kept knocking the agent's memory offline. Moved to Postgres.
- **Today:** company GBrain (Postgres; areas read, the Executive writes) + **Hindsight** as the graph for the Executive and Nara. Quality check scheduled for November 23.

## 7. Questions for Nous

1. How do you recommend **inventorying and monitoring routines** inside Hermes itself (cron, kanban, health), so we don't have to keep a table by hand?
2. What is the official way to make **WhatsApp listen-only**, and to also capture what the owner writes in groups?
3. **Memory:** is GBrain on Postgres + Hindsight the path you recommend for several area agents plus an owner's agent?
4. **Long conversations forget rules:** besides `sessions repair-prompts`, is there an official mechanism?
5. **Autonomy with safety:** how can agents configure themselves, fix their own routines, and publish to GitHub on their own (as the old agent did), with per-profile permissions and human approval only for sensitive actions? Is there a "maintainer agent" pattern?
6. **Model:** is `gpt-6.1-terra` (`high`) the right model for agents with many tools? Which of our errors would you attribute to the model, and which to our design?
