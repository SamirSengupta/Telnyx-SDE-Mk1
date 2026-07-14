# Quinn — Inbound Lead Handling (Growth Engineer Take-Home)

A stateful, multi-agent inbound SDR pipeline for Telnyx's AI SDR "Quinn":
capture a lead, enrich it, let a chain of AI agents decide how hot it is and
draft the outreach — then **stop and ask a human** before anything leaves the
building. Exactly-once side effects, full prompt-level observability, and a
CRM dashboard on top.

```
inbound form ──> enrich (DB) ──> qualify (LLM) ──> judge (LLM, veto power)
                                                        │
                              Cold ── SUPPRESSED  <─────┤
                                                        ▼
                        compose (LLM) ──> approve gate (policy + LLM)
                                                        │
                                                        ▼
                     Slack review card  ──  human types APPROVE
                                                        │
                                                        ▼
                       Gmail DRAFT created ── human checks it ── human SENDS
```

**The one non-negotiable:** a prospect can never be double-messaged. Every
side effect is claimed in a UNIQUE-keyed outbox ledger *before* it is
attempted — a duplicate send is structurally impossible, not just unlikely.

---

## Quickstart

Requirements: **Python 3.11+** and `pip install -r requirements.txt`
(pydantic is the only dependency — everything else is stdlib).

```bash
# 1. Prove the guarantees offline — no network, no keys needed:
python -m tests.test_pipeline      # 5 end-to-end tests

# 2. Seed the demo dataset (10 telecom-realistic leads):
python -m quinn.seed

# 3. Point Quinn at any OpenAI-compatible LLM endpoint:
cp .env.example .env               # then edit QUINN_PROXY_URL / QUINN_PROXY_KEY

# 4. Process the inbound queue:
python -m quinn.run --all

# 5. Open the CRM dashboard (second terminal):
python -m quinn.web                # -> http://localhost:8642

# 6. Approve outreach, one lead at a time (the human gate):
python -m quinn.run --review
```

> On Windows, `quinn.bat` wraps all of this in a 4-option menu:
> `crm` / `run` / `reset` / `obs`. (Use `py` instead of `python` if that's
> your launcher.)

**The idempotency demo:** run `python -m quinn.run --all` a second time —
0 additional LLM calls, 0 additional sends. Decisions are reused from the
database and the outbox refuses duplicates.

### LLM backend

All model traffic goes through one abstraction (`quinn/llm.py`) to an
OpenAI-compatible `/v1/chat/completions` endpoint (`QUINN_PROXY_URL`). It was
built against a local multi-LLM proxy (CLIProxyAPI) routing tasks to
different models:

| Layer | Default model | Why |
|-------|---------------|-----|
| Qualifier | claude-sonnet-4-6 | runs on every lead — speed/cost matter |
| Judge | claude-opus-4-6-thinking | strongest reasoner gets veto power |
| Composer | gemini-3.1-pro-low | different vendor = stylistic diversity |
| Approver | claude-sonnet-4-6 | reliable, conservative, cheap |

Every entry is overridable via env var — or live from the dashboard's
**Configure Agent** tab, which persists to `data/models.json` and applies to
the next lead with no restart.

### Integrations (both optional)

Without credentials, each channel automatically uses a safe offline logger —
the pipeline runs end-to-end with zero external side effects.

- **Slack**: set `QUINN_SLACK_WEBHOOK` (an Incoming Webhook). Review cards —
  tier, the qualifier's evidence, the judge's verdict, next step — and run
  summaries are posted to the channel.
- **Gmail**: point `QUINN_GMAIL_CREDS` at a Google OAuth *Desktop app* client
  JSON, then run `python -m quinn.gmail` once for consent. Scope is
  `gmail.compose` only (drafts + send — **no mailbox read**). The pipeline
  only ever creates **drafts**; sending is a separate human action.

---

## How it works

### Custom FSM orchestration (no agent framework)

`quinn/agent.py` drives each lead through an explicit state machine:

```
RECEIVED → ENRICHED → QUALIFIED → JUDGED → { SUPPRESSED | COMPOSED }
COMPOSED → { APPROVED | HELD }   APPROVED → DELIVERED → DONE
any transient state → FAILED (after the per-lead retry budget)
```

A `LEGAL` transition map rejects anything not explicitly allowed. Every
transition is persisted, so a crash resumes exactly where it stopped —
completed decisions are reused (no duplicate LLM spend), completed sends are
skipped (no duplicate outreach).

**`DONE` means "review card posted, awaiting a human" — not "email sent."**

### Every probabilistic step has a checker

- The **qualifier**'s tier is independently re-verified by the **judge** — a
  stronger model, temperature 0, prompted to be adversarial: verify every
  claim against the lead data, flag hallucinations, downgrade freely, veto to
  Cold on any disqualifier. Its verdict is authoritative.
- The **composer**'s draft passes the **approver**: a deterministic policy
  layer (valid recipient, suppression/opt-out list, no prior send, no
  unresolved placeholders) plus an LLM truthfulness review.
- The **FSM driver** validates every transition deterministically.

### The two-step human gate

The automated pipeline stops at a Slack review card. Then:

1. **Approve** (`--review` console, `--approve-mail ID`, or the dashboard) →
   the Gmail draft is created *now*, and the operator is prompted to check it
   in Gmail Drafts before the queue advances.
2. **Send** (`--send-mail ID` or the dashboard) → the checked draft goes out.

Reject at either step blocks that lead's email permanently. All three actions
ride the same outbox ledger — approving or sending twice is a no-op.

### Exactly-once delivery (the outbox pattern)

`quinn/integrations.py :: deliver_once` — for any (lead, channel):

1. **CLAIM** — atomic `INSERT` of a UNIQUE idempotency key. Losing the race
   means someone owns it already.
2. **SEND** — the notifier is called *with the same key*, so a provider that
   dedupes also collapses the "sent but the ack was lost" case.
3. **COMMIT** — record `sent` + provider message id, or `failed` (retryable).

### Observability, persisted

- **`llm_calls`** — one row per model invocation: task, requested vs. actual
  model, tokens, latency, attempt, outcome, and the **full system prompt,
  user prompt, and raw response**. Any decision is replayable from the DB.
- **`pipeline_events`** — every transition, decision, retry, delivery,
  explicit **tool call** (enrichment fetch, policy check, send-slack,
  send-email), and human action.

Surfaced via `--status`, `--trace <id>`, `--costs`, and the dashboard.

### CRM dashboard

`python -m quinn.web` → http://localhost:8642 — stdlib `http.server`, one
HTML file, zero JS dependencies.

| Tab | What it shows |
|-----|---------------|
| **Pipeline** | every lead: tier, state, topic, incumbent/volume, outreach status; click a row for the full journey — every decision with its rationale, the draft, and the outbox ledger |
| **Approvals** | the two human work queues with one-click Approve / Send / Reject (same code path as the CLI) |
| **Observability** | the agent-call ledger with expandable prompts/responses (paginated), the event stream, spend by stage |
| **Configure Agent** | per-layer model routing, editable live against the proxy's model catalog |

---

## Project structure

| Path | Responsibility |
|------|----------------|
| `quinn/llm.py` | **LLM abstraction** — the only module that talks to a model: task→model routing, pydantic-validated structured output, retries, per-call telemetry |
| `quinn/agent.py` | **FSM orchestration** — driver, state handlers, qualifier, Slack review card |
| `quinn/judge.py` | **LLM-as-judge** — independent tier verification with veto power |
| `quinn/email_composer.py` | outreach drafting — tier drives tone, topic drives template |
| `quinn/approver.py` | **approval gate** — deterministic policy checks + LLM truthfulness review |
| `quinn/integrations.py` | Slack/Gmail notifiers, `deliver_once` (outbox), the two-step human gate |
| `quinn/gmail.py` | Gmail OAuth (loopback flow) + draft/send/delete REST calls |
| `quinn/run.py` | CLI: drive/resume the queue, `--review` console, status/trace/costs |
| `quinn/web.py` + `quinn/static/index.html` | CRM dashboard + JSON API |
| `quinn/repo.py` | runtime data access (runs, decisions, telemetry, events, suppression) |
| `quinn/obs.py` | structured log lines + persisted event sink |
| `quinn/schemas.py` | pydantic contracts for every model↔pipeline boundary |
| `quinn/db.py` / `schema*.sql` | SQLite layer; input schema vs. runtime schema kept separate |
| `quinn/seed.py` / `seed_data.py` | 10 curated, telecom-realistic personas (deliberate ICP spread incl. noise & an opted-out lead) |
| `tests/test_pipeline.py` | 5 offline end-to-end tests (fake injected at the HTTP transport seam only) |
| `blueprint.md` | the orchestration blueprint — source of truth for control flow |
| `CLAUDE.md` | the AI-assistant project brief used while building (kept for transparency about the AI-assisted workflow) |

## CLI reference

```
python -m quinn.run --all             # process the whole inbound queue
python -m quinn.run --review          # interactive approval console
python -m quinn.run --status          # states, tiers, tokens, human queues
python -m quinn.run --trace 1         # full audit trail of one lead
python -m quinn.run --costs           # LLM spend per stage
python -m quinn.run --resume          # re-drive non-terminal runs
python -m quinn.run --reopen 7        # re-arm a HELD/FAILED run
python -m quinn.run --approve-mail 1  # human step 1: create the Gmail draft
python -m quinn.run --send-mail 1     # human step 2: send the checked draft
python -m quinn.run --reject-mail 1   # human: reject / discard
python -m quinn.web                   # CRM dashboard on :8642
python -m quinn.gmail                 # one-time Gmail OAuth consent
python -m tests.test_pipeline         # offline test suite
```

## Testing

```bash
python -m tests.test_pipeline
```

Five tests, fully offline. The fake is injected at exactly **one seam** — the
HTTP transport (`llm._post_chat`) — so routing, validation, telemetry, the
FSM, and the outbox all run for real. Covered: double-run idempotency (stable
sends *and* stable LLM spend), suppression compliance, delivery-failure
reconciliation on both channels, schema-violation retry, and the two-step
human gate (approve once, send once, reject blocks forever). Tests never load
`.env`, so they can never touch live Slack or Gmail.

## From POC to production

- **Postgres + a queue** instead of SQLite + in-process loop — the outbox
  protocol and unique keys port unchanged; claims gain a lease timeout for
  multi-worker reconciliation.
- **Slack app with interactive buttons** replacing the one-way webhook — the
  button payloads hit the same `human_approve` / `human_reject` functions.
- **Live enrichment** (Apollo/Clearbit) behind the same `LeadContext`.
- **OpenTelemetry** export — `llm_calls` rows already map 1:1 onto spans.
- **Eval harness** for tier accuracy — the append-only `decisions` table is
  the labeled dataset accumulating from day one.
- **Suppression sync** from ESP bounce/unsubscribe webhooks; per-domain rate
  limits.
