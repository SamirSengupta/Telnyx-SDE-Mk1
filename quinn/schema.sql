-- Quinn — Telnyx AI SDR
-- Data model for the inbound qualification path.
--
-- Two tables, deliberately kept separate:
--   1. inbound_requests — exactly what a prospect submits through a Telnyx
--      surface (web form, live chat, demo/pricing page). This is first-party,
--      self-reported, and often sparse.
--   2. enrichment — the "external extra info" layer. In production Quinn calls
--      data providers (Apollo / Clearbit / ZoomInfo) at request time. Here we
--      pre-seed fake-but-realistic rows so the AI agents have the firmographic
--      context they need to qualify ICP fit and intent without a live API.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS inbound_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    email        TEXT    NOT NULL,
    company      TEXT    NOT NULL,
    role         TEXT    NOT NULL,
    -- Free-text "What are you looking to build?" box on the form. Unstructured
    -- on purpose: this is the raw signal Quinn has to parse for product + intent.
    request_for  TEXT    NOT NULL,
    source       TEXT    NOT NULL,   -- web_form | live_chat | demo_request | pricing_page
    created_at   TEXT    NOT NULL    -- ISO-8601 UTC
);

CREATE TABLE IF NOT EXISTS enrichment (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_id            INTEGER NOT NULL UNIQUE
                          REFERENCES inbound_requests(id) ON DELETE CASCADE,
    person_name           TEXT    NOT NULL,
    role                  TEXT    NOT NULL,
    seniority             TEXT    NOT NULL,   -- IC | Manager | Director | VP | C-Level | Founder
    department            TEXT    NOT NULL,
    -- Estimated headcount under this person's department. A strong buying-power
    -- signal for the qualifier: a 40-person eng org buys differently than a 3-person one.
    dept_headcount        INTEGER NOT NULL,
    company               TEXT    NOT NULL,
    company_employees     INTEGER NOT NULL,
    industry              TEXT    NOT NULL,
    hq_region             TEXT    NOT NULL,
    estimated_revenue_usd TEXT    NOT NULL,   -- human-readable band, e.g. "$50M-$100M"
    current_provider      TEXT    NOT NULL,   -- incumbent CPaaS/telecom vendor, or "None / in-house"
    monthly_volume        TEXT    NOT NULL,   -- rough usage estimate (msgs / min / SIMs / DIDs)
    funding_stage         TEXT    NOT NULL,   -- Bootstrapped | Seed | Series A..D | Public | Private
    linkedin_url          TEXT
);

CREATE INDEX IF NOT EXISTS idx_enrichment_inbound ON enrichment(inbound_id);
