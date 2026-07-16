-- Quinn — knowledge base schema (grounding / retrieval layer).
--
-- WHAT THIS FILE DOES: defines the `knowledge` table — a small, curated store
-- of VERIFIED Telnyx product facts (each with a source URL) that the composer
-- and approver retrieve from at generation/review time. It exists so outreach
-- claims are grounded in a citable fact base rather than the model's parametric
-- memory, which can hallucinate or go stale. This is the third data concern,
-- deliberately separate from the input tables (schema.sql) and the pipeline
-- state tables (schema_runtime.sql): read-only reference knowledge.
--
-- Retrieval is keyword/topic based (stdlib SQL LIKE + topic match) — no vector
-- DB, consistent with the project's stdlib-first ethos. `tags` is a
-- space-separated keyword bag the retriever matches a lead's ask against.

CREATE TABLE IF NOT EXISTS knowledge (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT    NOT NULL,          -- messaging | voice | iot_connectivity
                                           -- | verify_2fa | porting_trunking
                                           -- | platform | compliance | coverage
    product     TEXT    NOT NULL,          -- short product/area label
    claim       TEXT    NOT NULL UNIQUE,   -- the fact, one sentence (UNIQUE -> seed idempotent)
    tags        TEXT    NOT NULL,          -- space-separated keywords for retrieval
    source_url  TEXT    NOT NULL,          -- provenance: where this fact came from
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_topic ON knowledge(topic);
