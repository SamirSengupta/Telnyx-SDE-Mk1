# `blueprint.md` — Quinn Inbound Orchestration (Source of Truth for Control Flow)

> This document is the **authoritative specification** for how the Quinn inbound
> lead-handling POC moves a lead from ingestion to outreach. `CLAUDE.md`
> describes *what* we're building and *why*; this document describes *how the
> pipeline runs* — the state machine, the module contracts, the persistence
> model, and the idempotency guarantees. Where the two disagree, the general
> principles in `CLAUDE.md` win on intent and this file wins on control flow.
>
> Everything here is designed to be **defensible on a live technical call**:
> every state transition, every side effect, and every failure mode has a
> single, explainable reason for existing.

---

## 0. Design goals (the five things a reviewer should hear us say)

1. **Exactly-once side effects.** A lead is never emailed or Slacked twice, even
   under retries, crashes, or concurrent workers. This is the headline
   requirement and it drives most of the architecture.
2. **Stateful, resumable pipeline.** Every meaningful decision is persisted
   *before* the next step runs. A crash at any point resumes from the last
   committed state with no duplicate work.
3. **Our own orchestration.** No off-the-shelf agent framework. The control flow
   is an explicit finite state machine (FSM) we own, drive, and can reason about
   line-by-line.
4. **Centralized, swappable model access.** All model calls go through `llm.py`
   → the multi-LLM proxy. Nothing else imports a provider SDK.
5. **Human-defended decisions.** Every tier assignment, judge verdict, and
   approval carries a stored rationale so it can be explained after the fact.

---

## 1. End-to-end flow (bird's-eye)

```
inbound_request
      │
      ▼
┌─────────────┐   read-only, no side effects
│  ENRICH     │   join inbound_requests × enrichment  → LeadContext
└─────┬───────┘
      ▼
┌─────────────┐   llm.py → proxy
│  QUALIFY    │   agent.py assigns tier + rationale + intent signals
└─────┬───────┘
      ▼
┌─────────────┐   llm.py → proxy (second opinion)
│  JUDGE      │   judge.py validates tier; may downgrade / flag
└─────┬───────┘
      ▼
   tier == Cold? ──yes──►┌──────────────┐
      │ no               │  SUPPRESS    │ terminal, no outreach
      ▼                  └──────────────┘
┌─────────────┐
│  COMPOSE    │   email_composer.py drafts tier-appropriate email
└─────┬───────┘
      ▼
┌─────────────┐   llm.py → proxy + policy checks
│  APPROVE    │   approver.py gates the send (the mail-approval agent)
└─────┬───────┘
      ▼
   approved? ──no──►┌──────────────┐
      │ yes         │  HELD        │ needs human; no send
      ▼             └──────────────┘
┌─────────────┐   integrations (4.3); idempotent claim → send → record
│  DELIVER    │   Slack notify (hot/warm) + email send, exactly once
└─────┬───────┘
      ▼
┌─────────────┐
│  DONE       │ terminal
└─────────────┘
```

The pipeline is a **state machine over a single `lead_run` row per inbound
lead**. Each box above is a state; each arrow is a transition guarded by a
condition and recorded atomically.

---

## 2. State machine (the contract)

### 2.1 States

| State | Kind | Side effects? | Meaning |
|-------|------|---------------|---------|
| `RECEIVED` | initial | none | Lead accepted into the pipeline; `lead_run` created. |
| `ENRICHED` | transient | none | Enrichment joined; `LeadContext` assembled. |
| `QUALIFIED` | transient | none | Tier + rationale assigned by the qualifier agent. |
| `JUDGED` | transient | none | Judge has confirmed / amended the tier. |
| `SUPPRESSED` | **terminal** | none | Tier = Cold (or judge veto). No outreach, by design. |
| `COMPOSED` | transient | none | Draft email produced for the lead's tier. |
| `APPROVED` | transient | none | Approver cleared the draft to send. |
| `HELD` | **terminal (parked)** | none | Approver blocked the send; awaits human review. |
| `DELIVERED` | transient | **yes** | Outreach dispatched exactly once (email ± Slack). |
| `DONE` | **terminal** | none | Run complete; outcome recorded. |
| `FAILED` | **terminal (parked)** | none | Unrecoverable error after retry budget; needs a human. |

> **Transient** states are checkpoints: the run pauses there only if the process
> dies. On resume, the orchestrator reads the current state and continues.
> **Terminal** states end the run. `HELD` and `FAILED` are terminal *for the
> automated pipeline* but can be re-opened by an operator.

### 2.2 Transitions

| From | Event / guard | To | Idempotency key |
|------|---------------|----|-----------------|
| `RECEIVED` | enrichment fetched OK | `ENRICHED` | `enrich:{inbound_id}` |
| `RECEIVED` | no enrichment row found | `ENRICHED` (degraded) | same; `enrichment_present=false` |
| `ENRICHED` | qualifier returns valid tier | `QUALIFIED` | `qualify:{inbound_id}` |
| `QUALIFIED` | judge agrees or amends | `JUDGED` | `judge:{inbound_id}` |
| `JUDGED` | `final_tier == Cold` | `SUPPRESSED` | — |
| `JUDGED` | `final_tier ∈ {Hot,Warm,Mild}` | `COMPOSED` | `compose:{inbound_id}` |
| `COMPOSED` | approver clears draft | `APPROVED` | `approve:{inbound_id}` |
| `COMPOSED` | approver blocks draft | `HELD` | — |
| `APPROVED` | delivery claim wins | `DELIVERED` | `deliver:{inbound_id}:{channel}` |
| any transient | retryable error | same state (retry) | — |
| any transient | error, retries exhausted | `FAILED` | — |
| `DELIVERED` | outcomes recorded | `DONE` | — |

**Legal-transition invariant:** the orchestrator refuses any transition not in
this table. An attempt to move `RECEIVED → DELIVERED` is a bug and raises, never
silently proceeds.

### 2.3 State diagram

```
RECEIVED ─► ENRICHED ─► QUALIFIED ─► JUDGED ─┬─► SUPPRESSED   (Cold)
                                             │
                                             └─► COMPOSED ─┬─► HELD   (blocked)
                                                           │
                                                           └─► APPROVED ─► DELIVERED ─► DONE

   any transient state ─► FAILED   (retries exhausted)
```

---

## 3. Persistence model (state lives in SQLite)

The existing data layer (`inbound_requests`, `enrichment`) is **read-only input**
to the pipeline. Orchestration adds new tables. Keep them in a new migration,
e.g. `quinn/schema_runtime.sql`, applied by `init_db` alongside `schema.sql`.

### 3.1 `lead_runs` — one row per lead, the FSM cursor

```sql
CREATE TABLE IF NOT EXISTS lead_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_id      INTEGER NOT NULL UNIQUE
                    REFERENCES inbound_requests(id) ON DELETE CASCADE,
    state           TEXT    NOT NULL DEFAULT 'RECEIVED',
    final_tier      TEXT,                 -- Hot | Warm | Mild | Cold (null until JUDGED)
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lead_runs_inbound ON lead_runs(inbound_id);
```

The `UNIQUE(inbound_id)` constraint is the first line of idempotency defense: a
lead can have **at most one run**. `INSERT OR IGNORE` on ingest means replaying
the same inbound lead never spawns a second pipeline.

### 3.2 `decisions` — append-only audit of every agent verdict

```sql
CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_id   INTEGER NOT NULL REFERENCES inbound_requests(id) ON DELETE CASCADE,
    stage        TEXT    NOT NULL,   -- qualify | judge | approve
    verdict      TEXT    NOT NULL,   -- tier, or pass/block, etc.
    rationale    TEXT    NOT NULL,   -- human-readable "why"
    model        TEXT    NOT NULL,   -- which proxied model produced it
    raw_json     TEXT    NOT NULL,   -- full structured model output
    created_at   TEXT    NOT NULL
);
```

Append-only. We never UPDATE a decision; a re-run writes a new row. This is the
"human-defended" audit trail.

### 3.3 `outbox` — the idempotency ledger for side effects

This table is the **heart of exactly-once**. Every side effect is claimed here
*before* it is attempted and recorded here *after* it completes.

```sql
CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT    NOT NULL UNIQUE,   -- e.g. deliver:{inbound_id}:email
    inbound_id      INTEGER NOT NULL REFERENCES inbound_requests(id) ON DELETE CASCADE,
    channel         TEXT    NOT NULL,          -- email | slack
    status          TEXT    NOT NULL,          -- claimed | sent | failed
    payload_hash    TEXT    NOT NULL,          -- hash of the exact content sent
    provider_msg_id TEXT,                      -- returned by integration on success
    claimed_at      TEXT    NOT NULL,
    completed_at    TEXT
);
```

The `UNIQUE(idempotency_key)` constraint is what makes double-send
*structurally impossible* — see §5.

---

## 4. Module contracts (one concern per file)

These match the target layout in `CLAUDE.md §3`. Each module exposes a small,
pure-ish interface; the orchestrator wires them together.

### 4.1 `llm.py` — the abstraction layer

The **only** module that talks to a model provider. Everything else depends on
this interface, never on a provider SDK.

```python
def complete(
    *,
    task: str,                 # logical task name, e.g. "qualify" | "judge" | "approve" | "compose"
    system: str,
    prompt: str,
    schema: dict | None = None,   # JSON schema for structured output, when needed
    temperature: float = 0.2,
) -> LLMResult: ...

@dataclass
class LLMResult:
    text: str
    data: dict | None          # parsed structured output when schema given
    model: str                 # concrete model the proxy routed to
    usage: dict                # tokens / latency for logging
```

Responsibilities:
- Route `task` → model via the **multi-LLM proxy** (config injected; *pending
  from user*). Until the proxy lands, ship a `FakeProxy` adapter behind the same
  interface so the FSM is fully testable offline.
- Enforce **structured output**: when `schema` is provided, validate the model's
  JSON and raise `LLMFormatError` on violation (retryable — see §6).
- Attach `model` + `usage` to every result for the audit trail.
- **No business logic.** It does not know what a "tier" is.

### 4.2 `agent.py` — the orchestrator + qualifier loop

Two responsibilities, kept explicit:

**(a) The FSM driver** — `run(inbound_id)`:
1. `get_or_create_run(inbound_id)` (INSERT OR IGNORE → `lead_runs`).
2. Loop: read current `state`, dispatch to that state's handler, persist the
   resulting transition atomically, repeat until terminal.
3. Every handler is a pure function `(LeadContext, run) -> (next_state, patch)`.
   The driver owns *all* writes so persistence stays in one place.

**(b) The qualifier** — the `ENRICHED → QUALIFIED` handler. Assembles the
`LeadContext`, calls `llm.complete(task="qualify", schema=TIER_SCHEMA)`, and
records a `decisions` row. Tooling available to the agent (as CLAUDE.md notes):
retrieve enrichment, look up prior state, trigger downstream actions. The
qualifier itself never sends anything — it only decides.

`TIER_SCHEMA` (structured output the qualifier must return):

```json
{
  "tier": "Hot | Warm | Mild | Cold",
  "icp_fit": "0.0–1.0",
  "intent": "0.0–1.0",
  "signals": ["short bullet evidence pulled from request_for + enrichment"],
  "disqualifiers": ["e.g. student, unsolicited-bulk-SMS, competitor, VC diligence"],
  "rationale": "2–4 sentences, defensible on a call"
}
```

### 4.3 `judge.py` — LLM-as-judge (second opinion)

Handler for `QUALIFIED → JUDGED`. Independent re-read of the same
`LeadContext` **plus** the qualifier's verdict. It can:
- **Confirm** the tier.
- **Amend** it (typically *downgrade* — the judge is a conservative gate, it may
  lower a tier but should justify any upgrade heavily).
- **Veto to Cold** if it spots a disqualifier the qualifier missed (competitor,
  spam, obvious non-buyer).

Uses a **different prompt and, ideally, a different routed model** than the
qualifier so the second opinion is genuinely independent (configured via the
`task` name in `llm.py`). Writes a `decisions` row (`stage="judge"`). The
`final_tier` written to `lead_runs` is the **judge's** output, not the
qualifier's.

Judge output schema:

```json
{
  "agree": true,
  "final_tier": "Hot | Warm | Mild | Cold",
  "changed": false,
  "reason": "why confirmed / amended / vetoed"
}
```

### 4.4 `email_composer.py` — draft the outreach

Handler for `COMPOSED`. Input: `LeadContext` + `final_tier`. Output: a draft
(`subject`, `body`, `channel_plan`). Tier controls tone and touch:

| Tier | Email? | Slack ping? | Tone |
|------|--------|-------------|------|
| Hot  | yes    | yes (high-priority to AE) | High-touch, specific to their stack (e.g. "replacing Twilio at ~2M SMS/mo"), fast CTA. |
| Warm | yes    | yes (standard) | Standard value-led, concrete next step. |
| Mild | yes    | no  | Light-touch / nurture; low-pressure resource + soft CTA. |
| Cold | —      | —   | Never reached (suppressed before compose). |

The composer must ground the email in **real fields** from enrichment
(`current_provider`, `monthly_volume`, `industry`, `dept_headcount`) so drafts
are specific, not generic. It produces content only; it does **not** send.

### 4.5 `approver.py` — the mail-approval agent (the gate)

Handler for `COMPOSED → APPROVED | HELD`. The final check before any side
effect. Two layers:

1. **Deterministic policy checks** (cheap, run first, no model):
   - Valid, non-internal recipient email; not on a suppression/opt-out list.
   - Required merge fields resolved (no `{{company}}` left in the body).
   - Tier is non-Cold and matches the composed channel plan.
   - No prior `sent` outbox row for this lead+channel (belt-and-suspenders vs §5).
2. **LLM review** via `llm.complete(task="approve")`: does the draft make false
   claims, mismatch the tier, or read as spam? Returns pass/block + reason.

Any failure → `HELD` (parked for a human), never a send. A pass → `APPROVED`.
Writes a `decisions` row (`stage="approve"`).

### 4.6 Integrations (4.3, *pending from user*) — `integrations/`

Two adapters behind a common `Notifier` interface, so the DELIVER state doesn't
know provider details:

```python
class Notifier(Protocol):
    def send(self, *, idempotency_key: str, payload: dict) -> DeliveryReceipt: ...
```

- `email_notifier.py` — sends the composed email.
- `slack_notifier.py` — posts the AE/rep notification for Hot/Warm.

Until real credentials land, ship `LoggingNotifier` implementations that record
to `outbox` and print, so the exactly-once path is fully exercised offline.

---

## 5. Idempotency — how exactly-once actually works (§the non-negotiable)

Three layers, defense in depth:

**Layer 1 — one run per lead.** `lead_runs.inbound_id` is `UNIQUE`. Ingest uses
`INSERT OR IGNORE`. Re-ingesting the same lead is a no-op.

**Layer 2 — the outbox claim/commit protocol.** Every side effect follows this
exact sequence inside `agent.py`'s DELIVER handler:

```
for channel in channels_for(final_tier):        # e.g. ["email"] or ["email","slack"]
    key = f"deliver:{inbound_id}:{channel}"

    # 2a. CLAIM — atomic. If another worker/retry already claimed, we lose the
    #     race cleanly and skip. This is the exactly-once pivot.
    try:
        INSERT INTO outbox(idempotency_key, inbound_id, channel, status,
                           payload_hash, claimed_at)
        VALUES (?, ?, ?, 'claimed', ?, now)
    except UNIQUE violation:
        continue          # someone already owns this send; do nothing

    # 2b. SEND — call the integration WITH the same idempotency_key so the
    #     provider also dedupes if we crash between send and record.
    receipt = notifier.send(idempotency_key=key, payload=payload)

    # 2c. COMMIT — record success (or 'failed' for retry).
    UPDATE outbox SET status='sent', provider_msg_id=?, completed_at=now
    WHERE idempotency_key=?
```

The **claim happens before the send**, and the claim is a unique-constraint
insert — so two concurrent workers, a retry after a crash, or a re-run can never
both pass 2a for the same key. At most one send per `(lead, channel)`, forever.

**Layer 3 — crash recovery is safe by construction.** If we die:
- *before 2a*: nothing happened; resume re-claims cleanly.
- *between 2a and 2c* (claimed but unknown outcome): on resume we find a
  `claimed` (not `sent`) row. We **do not blindly resend**. We reconcile: pass
  the same `idempotency_key` to the provider (which dedupes) or query its status,
  then set `sent`/`failed`. Passing the idempotency key to the integration is
  why a provider-side duplicate is also prevented, not just a DB-side one.

**Why `payload_hash`:** it lets us detect if a "resend" would differ from what
was claimed — a red flag we log rather than silently paper over.

State transitions themselves are committed in the **same SQLite transaction** as
their decision/outbox writes, so state and evidence can never diverge.

---

## 6. Failure handling & retries

| Failure | Class | Handling |
|---------|-------|----------|
| `LLMFormatError` (bad JSON from model) | retryable | Retry `complete()` up to N=2 with a stricter reminder; then `FAILED`. |
| Proxy timeout / 5xx | retryable | Exponential backoff, bounded by `attempt_count`; then `FAILED`. |
| Missing enrichment row | tolerated | Proceed in **degraded** mode; qualifier told enrichment is absent (usually pushes toward Mild/Cold). |
| Integration send error | retryable | Outbox row stays `claimed`→ set `failed`; re-run reconciles (§5 Layer 3). |
| Illegal state transition | bug | Raise immediately; do not proceed. |
| Approver block | expected | `HELD`, park for human — not a failure. |

`attempt_count` on `lead_runs` bounds total work per lead. Exhausting it moves
the run to `FAILED` with `last_error` set — a human queue, never a silent drop
and never a blind retry loop.

---

## 7. Orchestration entrypoints

```
py -m quinn.run --inbound-id 5      # drive one lead through the FSM
py -m quinn.run --all               # process the whole inbound queue
py -m quinn.run --resume            # re-drive any run in a non-terminal state
py -m quinn.run --status            # print each lead_run's state + final_tier
```

`--all` and `--resume` are safe to run repeatedly: idempotency (§5) guarantees
no duplicate outreach regardless of how many times they're invoked. This is the
property to demo live — run `--all` twice and show the outbox has exactly one
`sent` row per lead+channel.

---

## 8. Worked example (traceable end-to-end)

Lead **#1 — Marcus Reyes, VP Eng @ NimbusPay** ("Twilio bill out of hand,
~2M SMS/mo, want Verify API, US+LATAM deliverability").

1. `RECEIVED` — `lead_runs` row created for `inbound_id=1`.
2. `ENRICHED` — join yields: Series C fintech, 380 employees, 64-person eng org,
   incumbent **Twilio**, ~2M SMS/mo, Austin. Strong buying power + displacement
   signal.
3. `QUALIFIED` — qualifier returns `tier=Hot`, `icp_fit=0.9`, `intent=0.85`,
   signals `["displacing Twilio", "2M SMS/mo volume", "VP-level buyer",
   "explicit Verify API ask"]`. `decisions` row written.
4. `JUDGED` — judge confirms `Hot` (competitor displacement + volume + seniority
   all check out). `lead_runs.final_tier = Hot`.
5. `COMPOSED` — email grounded in "replacing Twilio at ~2M SMS/mo, Verify for
   2FA"; channel plan `["email","slack"]`.
6. `APPROVED` — policy checks pass (valid recipient, no stray merge fields, no
   prior send); LLM review passes.
7. `DELIVERED` — claim `deliver:1:email` and `deliver:1:slack`, send both,
   record `sent`. Re-running `--all` now hits the UNIQUE constraint on both keys
   and sends nothing.
8. `DONE`.

Contrast — a **Cold** lead (e.g. the student / unsolicited-bulk-SMS sender /
VC-diligence persona): stops at `JUDGED → SUPPRESSED`, no compose, no send.

---

## 9. What's stubbed vs. real (POC honesty)

| Piece | POC state | Prod swap |
|-------|-----------|-----------|
| Enrichment | pre-seeded rows | live Apollo/Clearbit/ZoomInfo call in the ENRICH handler |
| `llm.py` proxy | `FakeProxy` adapter until config lands | real multi-LLM proxy, same interface |
| Notifiers | `LoggingNotifier` → outbox | real Slack + email adapters (§4.6) |
| Store | local SQLite | same schema on a networked DB; outbox pattern is DB-agnostic |

The interfaces above are chosen so each swap is **drop-in**, not a rewrite — the
FSM, persistence, and idempotency logic don't change when the real integrations
arrive.

---

## 10. Build order (suggested, dependency-first)

1. `schema_runtime.sql` + extend `init_db` → `lead_runs`, `decisions`, `outbox`.
2. `llm.py` interface + `FakeProxy` (deterministic, offline-testable).
3. `agent.py` FSM driver + `RECEIVED/ENRICHED` handlers (no model yet).
4. Qualifier (`agent.py`) + `judge.py` against `FakeProxy`.
5. Suppression path + `email_composer.py`.
6. `approver.py` (policy checks first, then LLM review).
7. DELIVER handler + `LoggingNotifier` + full outbox protocol.
8. `quinn/run.py` CLI (`--all`, `--resume`, `--status`).
9. Tests: **idempotency test is the centerpiece** — run the pipeline twice,
   assert exactly one `sent` outbox row per lead+channel; crash-inject between
   claim and commit and assert no double-send on resume.

---

### Appendix A — invariants a reviewer can check in code

- No module except `llm.py` imports a model-provider SDK.
- No side effect occurs outside the outbox claim/commit protocol (§5).
- Every state write is in the same transaction as its evidence write.
- Only transitions in the §2.2 table are permitted; anything else raises.
- `final_tier` on `lead_runs` always equals the latest `judge` decision.
- Re-running any entrypoint produces zero additional `sent` rows.
