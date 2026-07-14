# Quinn — Inbound Lead Handling (Take-Home POC)

Telnyx's AI SDR, "Quinn," handles inbound and outbound sales end-to-end. This
repo is a **proof-of-concept for the inbound path**: capture a lead, enrich it,
let an AI system decide whether (and how hot) to reach out, and act — without
ever double-messaging a prospect.

> POC, not production. Fake-but-realistic data. The goal is to show engineering
> thinking and a defensible architecture, not a deployable service.

---

## 1. What we're building

A **stateful, multi-agent system for inbound lead handling.** End-to-end flow:

```
inbound request  ->  enrich (DB)  ->  AI system  ->  tier decision  ->  action (email / slack)
                                        │                                      │
                                        └── orchestrated by our own layer      └── idempotent: happens once
```

1. **Ingest** inbound data (the lead's self-reported form submission).
2. **Retrieve enrichment** for that lead from our database (external firmographic
   context — see the data model below).
3. **Send it through our AI system:**
   - **4.1 — Abstraction layer.** All model calls go through a single LLM
     abstraction. Behind it sits a **proxy running multiple LLMs**
     (CLIProxyAPI). Nothing else in the codebase talks to a model provider
     directly.
   - **4.2 — Evaluate & decide.** The system reads the request + enrichment,
     evaluates it, and decides whether to reach out. Every lead is sorted into
     one of **four tiers**:

     | Tier | Meaning | Rough intent |
     |------|---------|--------------|
     | **Hot**  | Strong ICP fit + clear buying intent | Reach out now, high touch |
     | **Warm** | Good fit, softer/near-term intent | Reach out, standard |
     | **Mild** | Marginal fit or unclear intent | Light-touch / nurture |
     | **Cold** | Poor fit, non-buyer, or disqualified | Do not reach out |

   - **Orchestration is our own.** We are not using an off-the-shelf agent
     framework — we build the orchestration ourselves. The workflow / state
     machine is specified in **`blueprint.md`** — the source of truth for
     control flow.
   - **4.3 — Integrations.** Slack (review cards + status) and Gmail
     (human-approved drafts) for the actual outreach.

4. **Idempotency (non-negotiable).** Every side-effecting operation must run
   **exactly once**. We must never send a client two emails for the same lead —
   it looks unprofessional. State is persisted so re-runs, retries, and crashes
   don't produce duplicate outreach. This is why the system is *stateful*.

---

## 2. Current state — built and working end-to-end

Everything below exists and is covered by the offline test suite
(`py -m tests.test_pipeline`, 5 tests):

- **FSM orchestration** (`agent.py`) — custom driver, no framework: legal-
  transition guard, per-lead retry budget, resume-skip guards (a re-run costs
  0 extra LLM calls and 0 extra sends).
- **Multi-LLM routing** (`llm.py`) — one abstraction over a local CLIProxyAPI:
  Sonnet qualifies/approves, Opus judges, Gemini composes.
- **Anti-drift checks at every probabilistic step** — the qualifier's tier is
  independently re-judged (`judge.py`: stronger model, temperature 0, veto
  power); the composed email passes a deterministic policy gate + an LLM
  truthfulness review (`approver.py`); every FSM transition is validated
  against the LEGAL map. No model output is acted on unchecked.
- **Two-step human gate** — the pipeline stops at a rich Slack review card
  (why the tier, judge's verdict, recommended next step). A human types
  `approve` in the review console (`py -m quinn.run --review`) or clicks it in
  the web UI → the Gmail draft is created → operator checks Drafts → only then
  does the queue advance. The actual send is a second explicit action
  (`--send-mail`); reject blocks the lead's email forever. All outbox-guarded.
- **Persisted observability** — `llm_calls` records every model call WITH the
  full system prompt, user prompt and raw response; `pipeline_events` records
  every transition/decision/retry/delivery/human action. Surfaced via
  `--status`, `--trace`, `--costs`, and the web UI's Observability tab.
- **CRM dashboard** (`py -m quinn.web` → http://localhost:8642) — black-and-
  white single-page UI (stdlib http.server, zero JS dependencies): Pipeline
  tab (all leads + full per-lead journey drawer), Approvals tab (the two-step
  gate with buttons), Observability tab (event stream + expandable prompts).

### Data layer (seeded)

| File | Purpose |
|------|---------|
| `quinn/schema.sql` | Input tables (below), kept deliberately separate |
| `quinn/schema_runtime.sql` | Pipeline state: `lead_runs`, `decisions`, `outbox`, `llm_calls`, `pipeline_events`, `suppression_list` |
| `quinn/db.py` | Dependency-free SQLite access layer + column migrations |
| `quinn/seed_data.py` | 10 curated, telecom-realistic personas (inbound + enrichment together) |
| `quinn/seed.py` | Splits each record into the two tables; idempotent (`py -m quinn.seed`) |
| `data/quinn.db` | Generated SQLite DB — 10 inbound rows + 10 enrichment rows |

### Data model

**`inbound_requests`** — first-party, self-reported, often sparse:
`id, name, email, company, role, request_for (free-text box), source, created_at`

**`enrichment`** — the "external extra info" layer (in prod: Apollo/Clearbit/
ZoomInfo at request time; here: pre-seeded):
`id, inbound_id (1:1 FK), person_name, role, seniority, department,
dept_headcount (employees under this person's dept), company, company_employees,
industry, hq_region, estimated_revenue_usd, current_provider (incumbent CPaaS —
Twilio/Vonage/Sinch/…), monthly_volume, funding_stage, linkedin_url`

The 10 seed rows are a **deliberate spread of ICP fit** (strong fits, mid fits,
and clear non-fits / noise like a student, a side-project, an unsolicited-bulk-
SMS sender, and a VC doing diligence) so the qualifier has real decisions to
make rather than rubber-stamping a uniformly hot list.

---

## 3. Module layout

Each responsibility lives in its **own file** — small, single-purpose, testable:

| Module | Responsibility |
|--------|----------------|
| `llm.py` | **Abstraction layer.** Single interface to the multi-LLM proxy; task→model routing; pydantic-validated structured output; per-call telemetry (incl. full prompts). |
| `agent.py` | **Custom FSM orchestration** — the driver, state handlers, qualifier, Slack review card. |
| `judge.py` | **LLM-as-judge.** Independently re-reads the lead + qualifier verdict; confirms, amends, or vetoes the tier. |
| `approver.py` | **Mail approval agent** — deterministic policy checks + LLM truthfulness review before any draft can proceed. |
| `email_composer.py` | Writes the outreach email for a lead + tier + topic (tier drives tone, topic drives template). |
| `integrations.py` | Slack/Gmail notifiers, `deliver_once` (exactly-once outbox), the two-step human gate (`human_approve` / `human_send` / `human_reject`). |
| `run.py` | CLI: drive/resume the queue, `--review` interactive approval console, status/trace/costs. |
| `web.py` + `static/index.html` | CRM dashboard + JSON API (stdlib http.server). |
| `repo.py` | Runtime data access (runs, decisions, telemetry, events, suppression). |
| `obs.py` | Structured log lines + persisted `pipeline_events` sink. |
| `schemas.py` | Pydantic contracts for every model↔pipeline boundary. |
| `gmail.py` | Gmail OAuth + draft/send/delete REST calls (drafts-only scope). |
| `db.py` / `seed.py` / `seed_data.py` | Storage + mock data. |

---

## 4. Principles & conventions

- **Idempotency everywhere.** Before any side effect (email/Slack), check
  persisted state; record the outcome atomically. One lead → at most one
  outreach per channel. Design for safe re-runs.
- **Stateful.** Decisions and actions are persisted in SQLite so the pipeline
  can resume, retry, and audit without duplicating work.
- **One concern per file.** Keep orchestration, model access, tooling, judging,
  approval, and composing separate.
- **Model access is centralized** through `llm.py` → the proxy. No direct
  provider SDK calls scattered around.
- **Human-defended.** Every non-trivial decision should be explainable on the
  live call — favor clarity over cleverness.

## 5. Environment

- Windows 11, **Python 3.11**. Invoke as `py` (not `python`). Run modules from
  repo root: `py -m quinn.<module>`.
- Stdlib-first: `sqlite3`, no ORM. Add dependencies only when they earn their keep.
- DB lives at `data/quinn.db`; re-seed anytime with `py -m quinn.seed`.

## 6. External inputs (all landed)

- [x] **LLM proxy** — CLIProxyAPI at `localhost:8317` (must be running for live
      runs; without it, decision stages fall back conservatively to Cold/HELD).
- [x] **`blueprint.md`** — the orchestration workflow / state machine
      (source of truth for control flow).
- [x] **Slack** — incoming webhook → `#all-telnyx-agent` (`QUINN_SLACK_WEBHOOK`
      in `.env`). One-way by design in this POC: approvals happen in the review
      console / web UI and every action is mirrored *to* Slack. Production path:
      a Slack app with interactive buttons (block actions) hitting the same
      `human_approve`/`human_reject` functions.
- [x] **Gmail** — OAuth installed-app flow, `gmail.compose` scope, drafts-only
      (`py -m quinn.gmail` for the one-time consent).

Credentials are gated on env vars loaded from `.env` by entrypoints only —
the test suite never imports `quinn.config`, so it can never touch live
Slack/Gmail.

## 7. Demo crib sheet

```
quinn demo                # fresh seed + process all 10 leads (proxy must be up)
py -m quinn.web           # CRM at http://localhost:8642 (second terminal)
py -m quinn.run --review  # type `approve` -> draft -> check Gmail Drafts -> next
py -m quinn.run --send-mail <id>   # final send after checking the draft
py -m quinn.run --all     # run twice: 0 extra LLM calls, 0 extra sends (idempotency)
py -m quinn.run --trace 1 # whole life of one lead from persisted state
py -m tests.test_pipeline # offline test suite (5 tests, no network)
```
