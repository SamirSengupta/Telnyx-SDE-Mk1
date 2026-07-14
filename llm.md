# `llm.md` — Model Access & the Anti-Hallucination Design

How Quinn talks to models, why every call goes through one file, and how a
second model keeps the first one honest. Pairs with `blueprint.md` (control
flow) — this doc is about the **model layer** specifically.

---

## 1. The abstraction layer (`quinn/llm.py`)

Every model call in the codebase goes through a single function:

```python
from quinn import llm

res = llm.complete(
    task="qualify",           # logical task -> routed to a concrete model
    system=SYSTEM_PROMPT,
    prompt=lead_block,
    schema=TIER_SCHEMA,       # ask for structured JSON; parsed into res.data
    temperature=0.1,
)
res.data   # -> dict (validated JSON)
res.model  # -> concrete model the proxy actually used
res.usage  # -> tokens / latency
```

`llm.py` is the **only** module that imports an HTTP client or knows a provider
name. Nothing else in the repo references a model string. Swapping models,
providers, or the proxy is a one-file change — this is the "model access is
centralized" principle from `CLAUDE.md`.

---

## 2. The proxy: CLIProxyAPI

We do **not** call Anthropic / OpenAI / etc. directly. All traffic goes through
**CLIProxyAPI** — a local, OpenAI-compatible multi-LLM proxy:

```
CLIProxyAPI — API server on :8317
Catalog on this box (GET /v1/models): claude-sonnet-4-6, claude-opus-4-6-thinking,
gemini-3.1-pro-low, gemini-3.x-flash family, gpt-oss-120b-medium, ...
```

Because it exposes the standard OpenAI shape, `llm.py` just POSTs to
`http://localhost:8317/v1/chat/completions`. One HTTP contract, many providers
behind it. "Use a different model" is only a different `model` string.

**Config (all env-overridable, sane defaults in code):**

| Env var | Default | Purpose |
|---------|---------|---------|
| `QUINN_PROXY_URL` | `http://localhost:8317/v1` | proxy base URL |
| `QUINN_PROXY_KEY` | `dummy` | Authorization header (this POC's proxy is a dummy/local instance and does not verify it, but the OpenAI-style header must be present) |
| `QUINN_LLM_TIMEOUT` | `60` | per-request timeout (s) |
| `QUINN_LLM_RETRIES` | `2` | bounded retries on transport/format errors |
| `QUINN_MODEL_QUALIFY` | `claude-sonnet-4-6` | qualifier model (fast workhorse) |
| `QUINN_MODEL_JUDGE` | `claude-opus-4-6-thinking` | judge model (**stronger critic**) |
| `QUINN_MODEL_COMPOSE` | `gemini-3.1-pro-low` | email composer (different vendor) |
| `QUINN_MODEL_APPROVE` | `claude-sonnet-4-6` | approval gate |

Model IDs come from the proxy's live catalog. To see everything available:

```bash
curl -s http://localhost:8317/v1/models -H "Authorization: Bearer dummy" | jq '.data[].id'
```

---

## 3. Task-based routing (the key idea)

Callers pass a **logical task**, not a model. `llm.py` maps it:

```python
TASK_MODELS = {
    "qualify": "claude-sonnet-4-6",         # fast, cheap first pass
    "judge":   "claude-opus-4-6-thinking",  # stronger critic <- has the last word
    "compose": "gemini-3.1-pro-low",        # different vendor for the writing step
    "approve": "claude-sonnet-4-6",
}
```

Routing is a **capability-asymmetry** design: the qualifier runs a cheap, fast
model on *every* lead, and the judge — a **stronger, more skeptical model** —
scrutinizes that verdict and has the last word. This is the classic
generator/critic split: spend the premium reasoning where it's a gate, not on
the first pass. The judge's independence here comes from being a *stronger tier*
+ a different prompt + `temperature=0`, rather than from a different vendor —
though provider diversity is one env var away (`QUINN_MODEL_JUDGE=...`) if you
want to A/B it. The composer deliberately sits on a different vendor (Gemini) so
the writing voice isn't the same model that judged the lead.

---

## 4. How the judge catches hallucination

Quinn's decision path is **two models, not one**:

```
LeadContext ──► QUALIFIER (claude-sonnet-4-6) ──► tier + rationale + signals
                                                          │
LeadContext + verdict ──► JUDGE (claude-opus-4-6-thinking) ──► confirm/amend/veto
                                                          │
                                                   final_tier (authoritative)
```

1. **Qualifier** (`agent.py`) reads the lead and assigns a tier with a
   structured rationale and the specific `signals` it relied on.
2. **Judge** (`judge.py`) re-reads the *same lead data* plus the qualifier's
   verdict, on a different model, and is instructed to:
   - verify every claim in the rationale against the actual lead fields;
   - emit `hallucination_flags` for any claim the data doesn't support;
   - **downgrade freely**, and **veto to Cold** for missed disqualifiers
     (competitor recon, unsolicited bulk SMS, student/side-project, VC
     diligence, obvious non-buyer);
   - only uphold a hot tier when ICP fit *and* intent are both evidenced.
3. The **judge's `final_tier` is what's written to `lead_runs`** — the judge has
   the last word, and both verdicts are stored in `decisions` (append-only) so
   the reasoning is auditable after the fact.

The judge runs at `temperature=0.0` (we want a stable, skeptical grader). If the
judge itself returns something unparseable, `_validate` never invents a hotter
tier — it falls back to the qualifier's tier or, failing that, `Cold`. The
system's failure mode is "don't send," never "send something wrong."

There's also a **third model gate**: the approver (`approver.py`) does a final
truthfulness/spam check on the *composed email* before anything is sent, after
cheap deterministic policy checks. So a message passes through qualify → judge →
approve before a single send.

---

## 5. Structured output & error handling

When `schema` is passed, `llm.py` instructs the model to reply with JSON only,
then parses and validates it (tolerating stray code fences / prose). Failures
are typed and **retryable**:

| Exception | Cause | Handling |
|-----------|-------|----------|
| `LLMTransportError` | proxy unreachable / timeout / 5xx | bounded exponential backoff, then surface |
| `LLMFormatError` | reply wasn't valid JSON for the schema | re-prompt harder, up to `QUINN_LLM_RETRIES` |

The orchestrator (`agent.py`) catches these per state and retries the *same*
state up to `MAX_RUN_ATTEMPTS`; exhaustion parks the run in `FAILED` (a human
queue) rather than guessing. No side effect ever happens on a failed decision.

---

## 6. Files at a glance

| File | Role in the model layer |
|------|-------------------------|
| `quinn/llm.py` | The abstraction layer + CLIProxyAPI transport + routing. **Only place that talks to a model.** |
| `quinn/agent.py` | FSM driver + **qualifier** (first opinion). |
| `quinn/judge.py` | **LLM-as-judge** second opinion on a different provider. |
| `quinn/email_composer.py` | Drafts the tier-appropriate email (never sends). |
| `quinn/approver.py` | Deterministic + LLM approval gate before send. |
| `quinn/integrations.py` | Slack/email adapters + exactly-once `deliver_once`. |
| `quinn/repo.py` | LeadContext assembly + runtime-table access. |
| `quinn/run.py` | CLI (`--all`, `--resume`, `--status`, `--inbound-id`). |

---

## 7. Running it

```bash
py -m quinn.seed            # (re)seed the 22-lead demo dataset
py -m quinn.run --all       # drive the whole queue through qualify→judge→approve→deliver
py -m quinn.run --all       # run again: 0 additional sends (idempotent)
py -m quinn.run --status    # per-lead state, tier, and outbox send count
```

Requires CLIProxyAPI listening on `:8317`. Point elsewhere with
`QUINN_PROXY_URL`. Everything else runs on the Python stdlib — no extra
dependencies.
