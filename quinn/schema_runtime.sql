-- Quinn — runtime / orchestration schema.
--
-- The two tables in schema.sql (inbound_requests, enrichment) are READ-ONLY
-- input to the pipeline. The tables here hold pipeline STATE. Keeping them in a
-- separate file makes the boundary explicit: data-layer vs. orchestration-layer.
--
-- Idempotency lives here:
--   * lead_runs.inbound_id  UNIQUE  -> at most one pipeline run per lead.
--   * outbox.idempotency_key UNIQUE -> at most one side effect per (lead, channel).

PRAGMA foreign_keys = ON;

-- One row per lead == the finite-state-machine cursor.
CREATE TABLE IF NOT EXISTS lead_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_id    INTEGER NOT NULL UNIQUE
                  REFERENCES inbound_requests(id) ON DELETE CASCADE,
    state         TEXT    NOT NULL DEFAULT 'RECEIVED',
    final_tier    TEXT,                       -- Hot | Warm | Mild | Cold (null until JUDGED)
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

-- Append-only audit of every agent verdict. Never UPDATEd; a re-run appends.
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_id  INTEGER NOT NULL REFERENCES inbound_requests(id) ON DELETE CASCADE,
    stage       TEXT    NOT NULL,             -- qualify | judge | approve | operator
    verdict     TEXT    NOT NULL,             -- tier, or pass/block
    rationale   TEXT    NOT NULL,             -- human-readable "why"
    model       TEXT    NOT NULL,             -- which proxied model produced it
    raw_json    TEXT    NOT NULL,             -- full structured model output
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_inbound ON decisions(inbound_id);

-- The idempotency ledger. A side effect is CLAIMED here before it is attempted
-- and COMMITTED here after it completes. The UNIQUE key makes a double-send
-- structurally impossible (see llm.md / blueprint.md §5).
CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT    NOT NULL UNIQUE,  -- e.g. deliver:{inbound_id}:email
    inbound_id      INTEGER NOT NULL REFERENCES inbound_requests(id) ON DELETE CASCADE,
    channel         TEXT    NOT NULL,         -- email | slack
    status          TEXT    NOT NULL,         -- claimed | sent | failed
    payload_hash    TEXT    NOT NULL,         -- hash of the exact content sent
    provider_msg_id TEXT,
    claimed_at      TEXT    NOT NULL,
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_inbound ON outbox(inbound_id);

-- Telemetry ledger: one row per model invocation (observability §3.1). Answers,
-- from persisted data alone: which model, how many tokens, how long, which
-- attempt, did it succeed — AND exactly what was said: the full system prompt,
-- user prompt, and raw response are captured so any decision can be replayed
-- from the DB alone. Maps 1:1 onto an OpenTelemetry span in prod (prompts as
-- span attributes / logged payloads).
CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_id        INTEGER REFERENCES inbound_requests(id) ON DELETE CASCADE,
    stage             TEXT,                 -- qualify | judge | compose | approve
    task              TEXT    NOT NULL,     -- logical task routed
    requested_model   TEXT    NOT NULL,     -- what TASK_MODELS asked for
    used_model        TEXT,                 -- what the proxy actually used
    prompt_tokens     INTEGER,              -- nullable if proxy omits usage
    completion_tokens INTEGER,
    latency_ms        INTEGER,
    attempt           INTEGER NOT NULL,     -- retry number (1-based)
    outcome           TEXT    NOT NULL,     -- ok | transport_error | format_error
    system_prompt     TEXT,                 -- exact system prompt sent (this attempt)
    user_prompt       TEXT,                 -- exact user prompt sent
    response_text     TEXT,                 -- raw model reply (before validation)
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_inbound ON llm_calls(inbound_id);

-- Observability event stream: every pipeline event (state transition, decision,
-- retry, delivery, human action) that obs.event() emits to the console is ALSO
-- persisted here — one grep-friendly log line, one queryable row. This is what
-- the UI's Observability tab and post-hoc audits read; nothing about a run is
-- reconstructible only from stdout.
CREATE TABLE IF NOT EXISTS pipeline_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_id  INTEGER,                    -- nullable: some events are global
    name        TEXT    NOT NULL,           -- transition | decision | delivery | ...
    fields_json TEXT    NOT NULL,           -- full structured payload of the event
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_inbound ON pipeline_events(inbound_id);

-- Compliance suppression / opt-out list (blueprint §4.5). A recipient here is
-- never emailed — a hard block in the approver, independent of tier. Matching is
-- by exact email or by domain (leading "@").
CREATE TABLE IF NOT EXISTS suppression_list (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern    TEXT    NOT NULL UNIQUE,     -- "user@x.com" or "@baddomain.com"
    reason     TEXT    NOT NULL,            -- opt-out | bounce | legal | do-not-contact
    created_at TEXT    NOT NULL
);
