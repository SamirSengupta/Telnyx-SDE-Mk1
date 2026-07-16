"""Quinn CRM dashboard — the human's window into everything the agents did.

What this file does: serves a single-page, black-and-white CRM UI
(``quinn/static/index.html``) plus a small JSON API over the same SQLite state
the pipeline writes. Nothing here re-computes anything — every panel is a
straight read of the persisted tables, which is the point: if it's on the
dashboard, it's in the database, and vice versa.

    py -m quinn.web            ->  http://localhost:8642

Surfaces:
  * Pipeline tab       — every lead: state, tier, topic, firmographics; click a
    row for the full journey (decisions with rationales, draft, deliveries).
  * Approvals tab      — the human work queue. Approve creates the Gmail draft
    (integrations.human_approve), then prompts the operator to check Drafts;
    Send fires the checked draft (human_send); Reject blocks the lead forever.
    Identical semantics to the --review console — both call the same functions,
    so the outbox guarantees hold no matter which surface the human uses.
  * Observability tab  — the persisted pipeline_events stream and the llm_calls
    ledger with the FULL prompts and raw responses of every model call.

Stdlib only (http.server + json), one thread per request; every request opens
its own SQLite connection, so there is no shared-connection threading hazard.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import quinn.config  # noqa: F401 — load .env first (Slack webhook, Gmail creds)
from quinn import llm
from quinn.agent import NEXT_STEP, human_rewrite, reopen, run_lead
from quinn.db import DB_PATH, get_connection, init_db
from quinn.integrations import (
    human_approve,
    human_reject,
    human_send,
    human_unreject,
)
from quinn.obs import set_event_sink, setup_logging
from quinn.repo import model_token_report, record_event

PORT = 8642
ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
LOGO = ROOT / "telnyx-logo.jpg"

# Human actions mutate state; serialize them so two browser clicks can't race.
# (Reads need no lock — SQLite handles concurrent readers fine.)
_action_lock = threading.Lock()

OUTREACH_ORDER = "CASE final_tier WHEN 'Hot' THEN 0 WHEN 'Warm' THEN 1 " \
                 "WHEN 'Mild' THEN 2 ELSE 3 END"


# --------------------------------------------------------------------------- #
# Query helpers — every endpoint is a plain read of the persisted tables.      #
# --------------------------------------------------------------------------- #

# One word for where a lead's email stands: none (nothing yet), drafted
# (in Gmail Drafts), sent, or rejected.
def _email_status(conn, iid: int) -> str:
    """One word for where the lead's email is in the two-step human gate."""
    srow = conn.execute("SELECT status FROM outbox WHERE idempotency_key=?",
                        (f"send:{iid}:email",)).fetchone()
    if srow and srow["status"] == "sent":
        return "sent"
    if srow and srow["status"] == "rejected":
        return "rejected"
    drow = conn.execute("SELECT status FROM outbox WHERE idempotency_key=?",
                        (f"deliver:{iid}:email",)).fetchone()
    if drow and drow["status"] == "sent":
        return "drafted"            # draft exists, awaiting check + final send
    return "none"


# Fetches one saved verdict's details as a plain dict (empty if none yet).
def _decision_doc(conn, iid: int, stage: str) -> dict:
    row = conn.execute("SELECT raw_json FROM decisions WHERE inbound_id=? AND "
                       "stage=? ORDER BY id DESC LIMIT 1", (iid, stage)).fetchone()
    try:
        return json.loads(row["raw_json"]) if row else {}
    except json.JSONDecodeError:
        return {}


# Feeds the stat tiles at the top of the dashboard (counts by tier, state,
# queue sizes, total AI spend).
def api_summary(conn, _q) -> dict:
    states = {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM lead_runs GROUP BY state")}
    tiers = {r[0]: r[1] for r in conn.execute(
        "SELECT final_tier, COUNT(*) FROM lead_runs "
        "WHERE final_tier IS NOT NULL GROUP BY final_tier")}
    calls, tokens = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(COALESCE(prompt_tokens,0)"
        "+COALESCE(completion_tokens,0)),0) FROM llm_calls").fetchone()
    total_leads = conn.execute("SELECT COUNT(*) FROM inbound_requests").fetchone()[0]
    cards = conn.execute("SELECT COUNT(*) FROM outbox WHERE channel='slack' "
                         "AND status='sent'").fetchone()[0]
    emails_sent = conn.execute("SELECT COUNT(*) FROM outbox WHERE "
                               "idempotency_key LIKE 'send:%' AND status='sent'"
                               ).fetchone()[0]
    runs = conn.execute("SELECT inbound_id FROM lead_runs WHERE state='DONE'").fetchall()
    awaiting_approval = [r["inbound_id"] for r in runs
                         if _email_status(conn, r["inbound_id"]) == "none"]
    awaiting_send = [r["inbound_id"] for r in runs
                     if _email_status(conn, r["inbound_id"]) == "drafted"]
    return {"states": states, "tiers": tiers, "llm_calls": calls,
            "tokens": tokens, "total_leads": total_leads,
            "slack_cards": cards, "emails_sent": emails_sent,
            "awaiting_approval": awaiting_approval,
            "awaiting_send": awaiting_send}


# Feeds the Pipeline table: one row per lead with tier, state, topic, and
# where its email stands.
def api_leads(conn, _q) -> list:
    rows = conn.execute(
        "SELECT lr.*, ir.name, ir.email, ir.company, ir.role, ir.request_for, "
        "ir.source, e.current_provider, e.monthly_volume, e.industry, "
        "e.company_employees "
        "FROM lead_runs lr "
        "JOIN inbound_requests ir ON ir.id = lr.inbound_id "
        "LEFT JOIN enrichment e ON e.inbound_id = lr.inbound_id "
        f"ORDER BY {OUTREACH_ORDER}, lr.inbound_id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["topic"] = _decision_doc(conn, r["inbound_id"], "qualify") \
            .get("primary_topic", "")
        d["email_status"] = _email_status(conn, r["inbound_id"])
        out.append(d)
    return out


# Feeds the full-journey drawer for one lead: their info, every decision with
# its reasoning, the draft, the grounding facts, deliveries, and events.
def api_lead(conn, q) -> dict:
    iid = int(q["id"][0])
    lead = conn.execute(
        "SELECT ir.*, e.person_name, e.seniority, e.department, e.dept_headcount, "
        "e.company_employees, e.industry, e.hq_region, e.estimated_revenue_usd, "
        "e.current_provider, e.monthly_volume, e.funding_stage, e.linkedin_url "
        "FROM inbound_requests ir LEFT JOIN enrichment e ON e.inbound_id = ir.id "
        "WHERE ir.id=?", (iid,)).fetchone()
    run = conn.execute("SELECT * FROM lead_runs WHERE inbound_id=?", (iid,)).fetchone()
    decisions = []
    for d in conn.execute("SELECT * FROM decisions WHERE inbound_id=? ORDER BY id",
                          (iid,)):
        doc = dict(d)
        try:
            doc["raw"] = json.loads(doc.pop("raw_json"))
        except json.JSONDecodeError:
            doc["raw"] = {}
        decisions.append(doc)
    calls = [dict(c) for c in conn.execute(
        "SELECT * FROM llm_calls WHERE inbound_id=? ORDER BY id", (iid,))]
    outbox = [dict(o) for o in conn.execute(
        "SELECT * FROM outbox WHERE inbound_id=? ORDER BY id", (iid,))]
    events = [dict(e) for e in conn.execute(
        "SELECT * FROM pipeline_events WHERE inbound_id=? ORDER BY id", (iid,))]
    approve_doc = _decision_doc(conn, iid, "approve")
    draft = approve_doc.get("draft", {})
    return {"lead": dict(lead) if lead else None,
            "run": dict(run) if run else None,
            "decisions": decisions, "llm_calls": calls, "outbox": outbox,
            "events": events, "draft": draft,
            # RAG grounding, surfaced: the exact verified facts (with source
            # URLs) this lead's draft was grounded on and checked against.
            "grounding": approve_doc.get("grounding_facts", []),
            "email_status": _email_status(conn, iid),
            "next_step": NEXT_STEP.get(run["final_tier"] if run else "", "")}


# Feeds the Observability event stream (optionally filtered to one lead).
def api_events(conn, q) -> list:
    lead = q.get("lead", [None])[0]
    if lead:
        rows = conn.execute("SELECT * FROM pipeline_events WHERE inbound_id=? "
                            "ORDER BY id DESC LIMIT 400", (int(lead),))
    else:
        rows = conn.execute("SELECT * FROM pipeline_events ORDER BY id DESC "
                            "LIMIT 400")
    return [dict(r) for r in rows]


# Feeds the Agent-calls list: every AI call with its full prompts and reply.
def api_llm(conn, q) -> list:
    lead = q.get("lead", [None])[0]
    if lead:
        rows = conn.execute("SELECT * FROM llm_calls WHERE inbound_id=? "
                            "ORDER BY id DESC LIMIT 200", (int(lead),))
    else:
        rows = conn.execute("SELECT * FROM llm_calls ORDER BY id DESC LIMIT 200")
    return [dict(r) for r in rows]


# Feeds the spend-by-stage table (calls, tokens, time per pipeline stage).
def api_costs(conn, _q) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT COALESCE(stage,task) stage, COUNT(*) calls, "
        "COALESCE(SUM(prompt_tokens),0) prompt_tokens, "
        "COALESCE(SUM(completion_tokens),0) completion_tokens, "
        "COALESCE(SUM(latency_ms),0) latency_ms "
        "FROM llm_calls GROUP BY COALESCE(stage,task) ORDER BY prompt_tokens DESC")]


# ---- Live database browser (the "it's all really in SQLite" surface) -------- #
# A read-only window onto the actual tables. Table/column names can't be bound
# as SQL parameters, so every name is validated against sqlite_master before it
# reaches a query string — no injection surface. The free-form query endpoint
# uses a MODE=RO connection, so even a typo'd DELETE physically cannot write.

# The list of real table names — used to reject any made-up name a request
# might try to sneak into a query.
def _table_names(conn) -> set:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}


# Feeds the Database tab's left rail: every table with its live row count.
def api_db_tables(conn, _q) -> list:
    """Every table with its live row count — the left rail of the DB tab."""
    out = []
    for name in sorted(_table_names(conn)):
        n = conn.execute(f"SELECT COUNT(*) c FROM {name}").fetchone()["c"]
        out.append({"name": name, "rows": n})
    return out


# Feeds the Database tab's main view: the newest rows of whichever table is
# selected (name checked against the real table list first).
def api_db_table(conn, q) -> dict:
    """Recent rows of one validated table (newest first when it has an id)."""
    name = q.get("name", [""])[0]
    if name not in _table_names(conn):
        return {"error": f"unknown table {name!r}"}
    limit = max(1, min(500, int(q.get("limit", ["50"])[0])))
    cols = [d[1] for d in conn.execute(f"PRAGMA table_info({name})")]
    order = "ORDER BY id DESC" if "id" in cols else ""
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM {name} {order} LIMIT ?", (limit,))]
    return {"name": name, "columns": cols, "rows": rows,
            "total": conn.execute(f"SELECT COUNT(*) c FROM {name}").fetchone()["c"]}




# ---- Configure Agent (the abstraction layer, made visible) ----------------- #
# Which layer does what, and what to weigh when picking its model. Pros/cons
# are keyed by model-id substring so new proxy models still get sane notes.
LAYERS = {
    "qualify": "Qualifier — first-pass tier + topic. Runs on every lead: speed "
               "and cost matter more than depth.",
    "judge":   "Judge — independent second opinion with veto power. The "
               "skeptic: reasoning quality matters most here.",
    "compose": "Composer — writes the outreach email. Style and specificity "
               "matter; mistakes are caught downstream by the approver.",
    "approve": "Approver — final truthfulness gate before any draft. Needs "
               "reliable, conservative judgment at low cost.",
}
MODEL_NOTES = [
    ("claude-opus", "Strongest reasoning and skepticism; best at catching "
                    "subtle hallucinations.", "Slowest and most expensive — "
                    "overkill for high-volume first passes."),
    ("claude-sonnet", "Fast, cheap, excellent structured-output discipline — "
                      "the workhorse tier.", "Less deep reasoning than Opus on "
                      "ambiguous judgment calls."),
    ("gemini", "Quick, stylistically diverse writing; different vendor adds "
               "diversity to the pipeline.", "JSON formatting slightly less "
               "consistent — relies on the retry loop."),
    ("gpt", "Solid general-purpose alternative; useful third opinion.",
            "No standout edge for these specific stages."),
]


# Looks up the plain-English strengths/weaknesses blurb for a model id.
def _notes_for(model_id: str) -> tuple[str, str]:
    for key, pros, cons in MODEL_NOTES:
        if key in model_id:
            return pros, cons
    return ("Available on the proxy.", "Unproven for this pipeline — test "
            "before relying on it.")


# Asks the model proxy which models it currently offers (for the dropdowns);
# falls back to the ones already configured if the proxy is unreachable.
def _available_models() -> list[str]:
    """Live model list from the proxy; falls back to the models already in use."""
    fallback = sorted({*llm.TASK_MODELS.values(), llm.DEFAULT_MODEL})
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{llm.PROXY_BASE_URL}/models",
            headers={"Authorization": f"Bearer {llm.PROXY_API_KEY}"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = sorted({m.get("id", "") for m in data.get("data", []) if m.get("id")})
        return ids or fallback
    except Exception:                               # noqa: BLE001 — proxy down
        return fallback


# Feeds the Configure Agent tab: each step's main + backup model, and the
# list of models you could switch to.
def api_config(_conn, _q) -> dict:
    overrides = llm.model_overrides()
    models = _available_models()
    return {
        "layers": [{"task": t, "about": about,
                    # primary model (runs the task)
                    "current": llm.model_for(t),
                    "default": llm.TASK_MODELS[t],
                    "overridden": t in overrides,
                    # backup model (fails over when the primary is down/capped)
                    "fallback_current": llm.fallback_for(t),
                    "fallback_default": llm.FALLBACK_MODELS.get(t),
                    "fallback_overridden": f"fallback:{t}" in overrides}
                   for t, about in LAYERS.items()],
        "models": [{"id": m, "pros": _notes_for(m)[0], "cons": _notes_for(m)[1]}
                   for m in models],
    }


# Feeds the token-bucket bars: each model's total spend vs its 100k cap.
def api_tokens(conn, _q) -> list:
    """Per-model token buckets: spend vs the hard budget (llm.TOKEN_BUDGET)."""
    return model_token_report(conn)


# Feeds the Rejected tab: every turned-down lead with its reason, ready for
# the Undo button.
def api_rejected(conn, _q) -> list:
    """The undo queue: every lead whose email was rejected, with the reason."""
    rows = conn.execute(
        "SELECT o.inbound_id, o.completed_at, ir.name, ir.company, ir.role, "
        "lr.final_tier FROM outbox o "
        "JOIN inbound_requests ir ON ir.id = o.inbound_id "
        "LEFT JOIN lead_runs lr ON lr.inbound_id = o.inbound_id "
        "WHERE o.idempotency_key LIKE 'send:%' AND o.status='rejected' "
        "ORDER BY o.completed_at DESC").fetchall()
    out = []
    for r in rows:
        d = conn.execute(
            "SELECT rationale FROM decisions WHERE inbound_id=? AND stage='human' "
            "AND verdict='reject' ORDER BY id DESC LIMIT 1",
            (r["inbound_id"],)).fetchone()
        out.append({**dict(r), "reason": d["rationale"] if d else ""})
    return out


GET_ROUTES = {
    "/api/summary": api_summary,
    "/api/leads": api_leads,
    "/api/lead": api_lead,
    "/api/events": api_events,
    "/api/llm": api_llm,
    "/api/costs": api_costs,
    "/api/config": api_config,
    "/api/tokens": api_tokens,
    "/api/rejected": api_rejected,
    "/api/db/tables": api_db_tables,
    "/api/db/table": api_db_table,
}

# The UI's action buttons call the SAME functions as the CLI — one code path,
# one set of idempotency guarantees, regardless of surface.
ACTIONS = {
    "approve": human_approve,                     # step 1: create Gmail draft
    "send": human_send,                           # step 2: fire the send
    "reject": lambda conn, iid: human_reject(conn, iid,
                                             "rejected by operator (web UI)"),
    "unreject": human_unreject,                   # undo a rejection
    # Re-arm: reopen AND immediately re-drive the lead to its new terminal state,
    # so the UI never leaves it stranded mid-pipeline in COMPOSED. It lands right
    # back in HELD (block still applies), or reaches DONE (fresh card) if it now
    # passes — either way the operator sees a final answer, not a limbo state.
    "reopen": lambda conn, iid: (reopen(conn, iid),
                                 run_lead(conn, iid, verbose=False))[1]["state"],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):              # quiet: keep demo stdout clean
        pass

    # ---- responses ----------------------------------------------------------
    # Sends a Python object back to the browser as JSON.
    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Sends a file (the page or the logo) back to the browser, uncached.
    def _file(self, path: Path, ctype: str):
        if not path.exists():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # no-store: the dashboard HTML must never be served stale from browser
        # cache — a UI update should be one plain refresh away, not Ctrl+F5.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- routing ------------------------------------------------------------
    # Answers every read: the page itself, the logo, and all /api/... data.
    # Opens a fresh DB connection per request (thread-safe by construction).
    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._file(STATIC / "index.html", "text/html; charset=utf-8")
        if url.path == "/logo":
            return self._file(LOGO, "image/jpeg")
        fn = GET_ROUTES.get(url.path)
        if fn is None:
            return self.send_error(404)
        conn = get_connection(DB_PATH)
        try:
            return self._json(fn(conn, parse_qs(url.query)))
        except Exception as exc:                    # noqa: BLE001
            return self._json({"error": str(exc)}, 500)
        finally:
            conn.close()

    # Answers every button click: model config changes and the human actions
    # (approve / send / reject / unreject / rewrite / re-arm). Actions run
    # under a lock so two clicks can't race, and they call the SAME functions
    # the CLI uses — one code path, one set of safety guarantees.
    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        if url.path == "/api/config":
            # Persist a model override for one layer (model=null clears it).
            # kind='primary' (default) or 'fallback' picks which slot to set.
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
                kind = req.get("kind", "primary")
                llm.set_model_override(req["task"], req.get("model"), kind)
                return self._json({"ok": True, "task": req["task"], "kind": kind,
                                   "current": llm.model_for(req["task"]),
                                   "fallback": llm.fallback_for(req["task"])})
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                return self._json({"error": f"bad request: {exc}"}, 400)
        if url.path != "/api/action":
            return self.send_error(404)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
            action, iid = req["action"], int(req["id"])
            fn = None if action == "rewrite" else ACTIONS[action]
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            return self._json({"error": f"bad request: {exc}"}, 400)
        conn = get_connection(DB_PATH)
        set_event_sink(partial(record_event, conn))   # human actions are events too
        try:
            with _action_lock:
                if action == "rewrite":
                    # Recompose with operator feedback — returns the new draft's
                    # subject + whether it passed the approval gate.
                    res = human_rewrite(conn, iid, req.get("feedback", ""))
                    return self._json({"id": iid, "action": action, **res})
                status = fn(conn, iid)
            return self._json({"id": iid, "action": action, "status": status})
        except Exception as exc:                    # noqa: BLE001
            return self._json({"error": str(exc)}, 500)
        finally:
            set_event_sink(None)
            conn.close()


# Starts the dashboard server on localhost:8642 until Ctrl+C.
def main() -> None:
    setup_logging()
    conn = get_connection()
    init_db(conn)                                   # migrations before first read
    conn.close()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Quinn CRM dashboard -> http://localhost:{PORT}   (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
