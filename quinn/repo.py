"""Runtime data access + LeadContext assembly.

All reads/writes for the orchestration tables (lead_runs, decisions, outbox) go
through here so persistence stays in one place (mirrors how db.py centralizes
connection handling). Agents receive a plain :class:`LeadContext` and never
touch SQL themselves.
"""

from __future__ import annotations

import datetime as _dt
import json

# LeadContext now lives with the other typed contracts (schemas.py) as a frozen
# pydantic model. Re-exported here so existing `from quinn.repo import
# LeadContext` imports keep working.
from quinn.schemas import LeadContext

__all__ = ["LeadContext"]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# LeadContext assembly                                                         #
# --------------------------------------------------------------------------- #

def load_lead_context(conn, inbound_id: int) -> LeadContext:
    """Join inbound_requests × enrichment into a LeadContext (ENRICH step)."""
    row = conn.execute(
        "SELECT * FROM inbound_requests WHERE id = ?", (inbound_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no inbound_request with id={inbound_id}")

    enr = conn.execute(
        "SELECT * FROM enrichment WHERE inbound_id = ?", (inbound_id,)
    ).fetchone()

    common = dict(
        inbound_id=inbound_id,
        name=row["name"],
        email=row["email"],
        company=row["company"],
        role=row["role"],
        request_for=row["request_for"],
        source=row["source"],
        created_at=row["created_at"],
    )
    if enr is None:
        return LeadContext(enrichment_present=False, **common)
    return LeadContext(
        enrichment_present=True,
        seniority=enr["seniority"],
        department=enr["department"],
        dept_headcount=enr["dept_headcount"],
        company_employees=enr["company_employees"],
        industry=enr["industry"],
        hq_region=enr["hq_region"],
        estimated_revenue_usd=enr["estimated_revenue_usd"],
        current_provider=enr["current_provider"],
        monthly_volume=enr["monthly_volume"],
        funding_stage=enr["funding_stage"],
        linkedin_url=enr["linkedin_url"],
        **common,
    )


# --------------------------------------------------------------------------- #
# lead_runs — the FSM cursor                                                   #
# --------------------------------------------------------------------------- #

def get_or_create_run(conn, inbound_id: int) -> dict:
    """Idempotent run creation. UNIQUE(inbound_id) => at most one run per lead."""
    now = _now()
    conn.execute(
        """INSERT OR IGNORE INTO lead_runs (inbound_id, state, created_at, updated_at)
           VALUES (?, 'RECEIVED', ?, ?)""",
        (inbound_id, now, now),
    )
    conn.commit()
    return get_run(conn, inbound_id)


def get_run(conn, inbound_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM lead_runs WHERE inbound_id = ?", (inbound_id,)
    ).fetchone()
    return dict(row) if row else None


def update_run(conn, inbound_id: int, **fields) -> None:
    """Patch a lead_run row (state / final_tier / attempt_count / last_error)."""
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE lead_runs SET {cols} WHERE inbound_id = ?",
        (*fields.values(), inbound_id),
    )
    conn.commit()


def list_runs(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM lead_runs ORDER BY inbound_id"
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# decisions — append-only audit                                               #
# --------------------------------------------------------------------------- #

def record_decision(conn, inbound_id: int, stage: str, verdict: str,
                    rationale: str, model: str, raw_json: str) -> None:
    conn.execute(
        """INSERT INTO decisions
             (inbound_id, stage, verdict, rationale, model, raw_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (inbound_id, stage, verdict, rationale, model, raw_json, _now()),
    )
    conn.commit()


def get_decision(conn, inbound_id: int, stage: str) -> dict | None:
    """Return the latest decision row for a (lead, stage), or None.

    Used by the resume-skip guard: if a stage already produced a decision, the
    driver reuses it instead of re-calling the model on resume (no duplicate
    LLM spend, exact 'resumable' semantics).
    """
    row = conn.execute(
        "SELECT * FROM decisions WHERE inbound_id=? AND stage=? "
        "ORDER BY id DESC LIMIT 1", (inbound_id, stage)
    ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# llm_calls — telemetry ledger (observability)                                #
# --------------------------------------------------------------------------- #

def record_llm_call(conn, row: dict) -> None:
    """Telemetry sink installed into llm.set_recorder. Best-effort by contract:
    llm._record swallows exceptions so a telemetry write never fails a run.
    Captures the full conversation (system/user prompt + raw response), not just
    counters — the observability tab replays any decision from these rows."""
    conn.execute(
        """INSERT INTO llm_calls
             (inbound_id, stage, task, requested_model, used_model,
              prompt_tokens, completion_tokens, latency_ms, attempt, outcome,
              system_prompt, user_prompt, response_text, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row.get("inbound_id"), row.get("stage"), row.get("task"),
         row.get("requested_model"), row.get("used_model"),
         row.get("prompt_tokens"), row.get("completion_tokens"),
         row.get("latency_ms"), row.get("attempt"), row.get("outcome"),
         row.get("system_prompt"), row.get("user_prompt"),
         row.get("response_text"), _now()),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# pipeline_events — persisted observability stream                             #
# --------------------------------------------------------------------------- #

def record_event(conn, name: str, fields: dict) -> None:
    """Persist one pipeline event (installed into obs.set_event_sink).

    Same best-effort contract as record_llm_call: obs.event swallows sink
    exceptions, so an observability write can never fail a pipeline run."""
    conn.execute(
        "INSERT INTO pipeline_events (inbound_id, name, fields_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (fields.get("inbound_id"), name,
         json.dumps(fields, default=str), _now()),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# outbox / suppression reads (approver policy checks)                          #
# --------------------------------------------------------------------------- #

def prior_send_exists(conn, inbound_id: int, channel: str = "email") -> bool:
    """True if a completed send already exists for this (lead, channel).

    Belt-and-suspenders against double-send: deliver_once enforces this via the
    UNIQUE key, and the approver reads it too (blueprint §4.5)."""
    row = conn.execute(
        "SELECT 1 FROM outbox WHERE inbound_id=? AND channel=? AND status='sent' LIMIT 1",
        (inbound_id, channel),
    ).fetchone()
    return row is not None


def is_suppressed(conn, email: str) -> str | None:
    """Return the suppression reason if `email` (or its domain) is opted out."""
    email = (email or "").lower().strip()
    if not email:
        return None
    domain = "@" + email.split("@", 1)[1] if "@" in email else email
    row = conn.execute(
        "SELECT reason FROM suppression_list WHERE lower(pattern) IN (?, ?) LIMIT 1",
        (email, domain),
    ).fetchone()
    return row["reason"] if row else None


def add_suppression(conn, pattern: str, reason: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO suppression_list (pattern, reason, created_at) "
        "VALUES (?, ?, ?)",
        (pattern, reason, _now()),
    )
    conn.commit()
