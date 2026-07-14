"""Quinn's custom orchestration — the finite-state-machine driver + qualifier.

We do NOT use an off-the-shelf agent framework. Control flow is an explicit FSM
we own: each state has a handler `(conn, ctx, run) -> (next_state, patch)`, and
this driver owns every write so persistence + idempotency stay in one place.

States (see blueprint.md §2):
    RECEIVED -> ENRICHED -> QUALIFIED -> JUDGED -> {SUPPRESSED | COMPOSED}
    COMPOSED -> {APPROVED | HELD};  APPROVED -> DELIVERED -> DONE
    any transient -> FAILED (retries exhausted)

Terminal states: SUPPRESSED, HELD, DELIVERED->DONE, FAILED.

DONE means "Slack review card posted, awaiting a human" — NOT "email sent".
The automated pipeline stops at the card. Creating the Gmail draft is the
human's `approve` action, and the final send is a second human action; both
live in integrations.py (human_approve / human_send) and both are outbox-
guarded, so neither can ever happen twice.

Resumability: each decision stage (qualify/judge/approve) is guarded — if a
`decisions` row already exists for (inbound_id, stage), the driver reuses it and
transitions WITHOUT re-calling the model. So a crash after a decision is
persisted but before the state write costs zero duplicate LLM spend on resume.
"""

from __future__ import annotations

import functools
import json
import logging
import time
import traceback

from quinn import approver, email_composer, judge, llm, repo
from quinn.db import get_connection, init_db
from quinn.integrations import deliver_once
from quinn.obs import event, set_event_sink, setup_logging
from quinn.repo import (
    get_decision,
    get_or_create_run,
    get_run,
    load_lead_context,
    record_decision,
    record_event,
    record_llm_call,
    update_run,
)
from quinn.schemas import LeadContext, TierVerdict

_log = logging.getLogger("quinn.agent")

# --------------------------------------------------------------------------- #
# Legal transitions — the driver refuses anything not listed here.            #
# --------------------------------------------------------------------------- #
LEGAL = {
    "RECEIVED":  {"ENRICHED", "FAILED"},
    "ENRICHED":  {"QUALIFIED", "FAILED"},
    "QUALIFIED": {"JUDGED", "FAILED"},
    "JUDGED":    {"SUPPRESSED", "COMPOSED", "FAILED"},
    "COMPOSED":  {"APPROVED", "HELD", "FAILED"},
    "APPROVED":  {"DELIVERED", "FAILED"},
    "DELIVERED": {"DONE"},
}
TERMINAL = {"SUPPRESSED", "HELD", "DONE", "FAILED"}

# attempt_count is a per-lead LIFETIME retry budget across ALL states, not a
# per-state counter. This is deliberate: it bounds total spend/work per lead so a
# lead that keeps failing in different states can't loop forever. Exhausting it
# parks the run in FAILED for a human.
MAX_RUN_ATTEMPTS = 3

TIERS = ("Hot", "Warm", "Mild", "Cold")

# --------------------------------------------------------------------------- #
# Qualifier (the ENRICHED -> QUALIFIED brain)                                  #
# --------------------------------------------------------------------------- #
QUALIFIER_SYSTEM = (
    "You are Quinn, Telnyx's inbound SDR qualifier. Telnyx sells programmable "
    "voice & SMS, SIP trunking, number porting, IoT SIMs, and Verify/2FA to "
    "businesses. Read the lead and sort it into exactly one tier:\n"
    "  Hot  = strong ICP fit AND clear buying intent (reach out now, high touch)\n"
    "  Warm = good fit, softer/near-term intent (reach out, standard)\n"
    "  Mild = marginal fit or unclear intent (light-touch / nurture)\n"
    "  Cold = poor fit, non-buyer, or disqualified (do NOT reach out)\n"
    "Base every claim on the given data — do not invent facts. Watch for "
    "disqualifiers: competitors/CPaaS vendors doing recon, unsolicited bulk-SMS "
    "senders, students or hobby side-projects, VC diligence, obvious non-buyers.\n"
    "Also classify primary_topic — the main reason they reached out — so the "
    "right outreach template is used: messaging | voice | iot_connectivity | "
    "verify_2fa | porting_trunking | platform_other."
)


def qualify(ctx: LeadContext) -> tuple[TierVerdict, str]:
    """Return (validated TierVerdict, model_used)."""
    prompt = ctx.as_prompt_block() + "\n\nAssign the tier. Return JSON."
    try:
        res = llm.complete(task="qualify", system=QUALIFIER_SYSTEM, prompt=prompt,
                           schema=TierVerdict, temperature=0.1,
                           inbound_id=ctx.inbound_id, stage="qualify")
        return res.data, res.model
    except llm.LLMError:
        # Unparseable/unavailable after retries -> safest default is no outreach.
        return TierVerdict(tier="Cold", icp_fit=0.0, intent=0.0, signals=[],
                           disqualifiers=["qualifier unavailable"],
                           rationale="qualifier call failed after retries"), "fallback"


# --------------------------------------------------------------------------- #
# State handlers — each returns (next_state, patch_dict_for_lead_runs)        #
# --------------------------------------------------------------------------- #

def _h_received(conn, ctx, run):
    # ENRICH: context is already assembled by the driver; nothing else to do.
    return "ENRICHED", {}


def _h_enriched(conn, ctx, run):
    if get_decision(conn, ctx.inbound_id, "qualify") is None:   # resume-skip guard
        verdict, model = qualify(ctx)
        record_decision(conn, ctx.inbound_id, "qualify", verdict.tier,
                        verdict.rationale, model, verdict.model_dump_json())
        event(_log, "decision", inbound_id=ctx.inbound_id, stage="qualify",
              verdict=verdict.tier, model=model)
    else:
        event(_log, "decision_reused", inbound_id=ctx.inbound_id, stage="qualify")
    return "QUALIFIED", {}


def _h_qualified(conn, ctx, run):
    prior = get_decision(conn, ctx.inbound_id, "judge")
    if prior is not None:                                       # resume-skip guard
        event(_log, "decision_reused", inbound_id=ctx.inbound_id, stage="judge")
        return "JUDGED", {"final_tier": prior["verdict"]}
    qv = _latest_decision(conn, ctx.inbound_id, "qualify")
    verdict, model = judge.judge(ctx, qv)
    record_decision(conn, ctx.inbound_id, "judge", verdict.final_tier,
                    verdict.reason, model, verdict.model_dump_json())
    event(_log, "decision", inbound_id=ctx.inbound_id, stage="judge",
          verdict=verdict.final_tier, changed=verdict.changed,
          flags=len(verdict.hallucination_flags), model=model)
    return "JUDGED", {"final_tier": verdict.final_tier}


def _h_judged(conn, ctx, run):
    tier = run["final_tier"]
    if tier == "Cold":
        event(_log, "suppressed", inbound_id=ctx.inbound_id, tier=tier)
        return "SUPPRESSED", {}
    return "COMPOSED", {}


def _h_composed(conn, ctx, run):
    tier = run["final_tier"]
    prior = get_decision(conn, ctx.inbound_id, "approve")
    if prior is not None:                                       # resume-skip guard
        payload = json.loads(prior["raw_json"])
        event(_log, "decision_reused", inbound_id=ctx.inbound_id, stage="approve")
        if prior["verdict"] != "pass":
            return "HELD", {}
        run["_draft"] = payload["draft"]
        return "APPROVED", {}

    # Topic-routed template: the qualifier classified WHY they reached out.
    topic = _latest_decision(conn, ctx.inbound_id, "qualify") \
        .get("primary_topic", "platform_other")
    draft, cmodel = email_composer.compose(ctx, tier, topic)
    verdict, amodel = approver.approve(conn, ctx, tier, draft)
    draft_record = {**draft.model_dump(), "topic": topic,
                    "channels": email_composer.channels_for(tier)}
    record_decision(conn, ctx.inbound_id, "approve",
                    "pass" if verdict.approved else "block",
                    verdict.reason, amodel,
                    json.dumps({"approval": verdict.model_dump(),
                                "draft": draft_record, "compose_model": cmodel}))
    event(_log, "decision", inbound_id=ctx.inbound_id, stage="approve",
          verdict="pass" if verdict.approved else "block", model=amodel)
    if not verdict.approved:
        return "HELD", {}
    run["_draft"] = draft_record
    return "APPROVED", {}


def _h_approved(conn, ctx, run):
    # HUMAN GATE (the workflow's stop-the-line moment): the pipeline delivers
    # ONLY the Slack review card here — tier, the qualifier's why, the judge's
    # independent check, and the recommended next step — then parks. The Gmail
    # draft is NOT created yet; that is the human's "approve" action
    # (integrations.human_approve, driven from `--review` or the web UI), and
    # the final send is a second explicit human action (human_send). So:
    #   DELIVERED/DONE = "review card posted, awaiting a human approve".
    draft = run.get("_draft") or _latest_draft(conn, ctx.inbound_id)
    tier = run["final_tier"]
    payload = _payload_for("slack", ctx, tier, draft, conn)
    status = deliver_once(conn, ctx.inbound_id, "slack", payload)  # exactly-once
    event(_log, "delivery", inbound_id=ctx.inbound_id, channel="slack", status=status)
    # Only advance once the card is confirmed 'sent'. A 'failed'/'claimed' status
    # means it did not complete — raise so the driver retries this state (and
    # ultimately parks the run in FAILED). deliver_once is idempotent, so a
    # retry can never double-post the card.
    if status != "sent":
        raise RuntimeError(f"slack review card delivery incomplete ({status})")
    return "DELIVERED", {}


def _h_delivered(conn, ctx, run):
    return "DONE", {}


HANDLERS = {
    "RECEIVED": _h_received,
    "ENRICHED": _h_enriched,
    "QUALIFIED": _h_qualified,
    "JUDGED": _h_judged,
    "COMPOSED": _h_composed,
    "APPROVED": _h_approved,
    "DELIVERED": _h_delivered,
}


# --------------------------------------------------------------------------- #
# The driver                                                                  #
# --------------------------------------------------------------------------- #

def run_lead(conn, inbound_id: int, *, verbose: bool = True) -> dict:
    """Drive one lead through the FSM until it reaches a terminal state.

    Safe to call repeatedly: creation is idempotent, terminal runs are no-ops,
    decision stages are resume-skip guarded, and side effects are outbox-guarded
    — re-running never double-sends and never double-spends on models.
    """
    setup_logging()
    # Install the telemetry sinks (single choke points: llm.py for model calls,
    # obs.py for pipeline events). Every LLM call — prompt included — and every
    # FSM event is persisted for the observability surfaces (--trace, web UI).
    llm.set_recorder(functools.partial(record_llm_call, conn))
    set_event_sink(functools.partial(record_event, conn))

    ctx = load_lead_context(conn, inbound_id)
    # Tool call #1 of every run: the enrichment lookup (in prod: Apollo/Clearbit
    # at request time). Recorded like every other tool so the observability
    # stream shows tools, not just model calls.
    event(_log, "tool_call", inbound_id=inbound_id, tool="fetch_enrichment",
          found=ctx.enrichment_present)
    run = dict(get_or_create_run(conn, inbound_id))

    while run["state"] not in TERMINAL:
        state = run["state"]
        handler = HANDLERS[state]
        t0 = time.monotonic()
        try:
            next_state, patch = handler(conn, ctx, run)
        except Exception as exc:                    # noqa: BLE001
            attempts = run["attempt_count"] + 1
            if attempts >= MAX_RUN_ATTEMPTS:
                update_run(conn, inbound_id, state="FAILED",
                           attempt_count=attempts, last_error=str(exc))
                event(_log, "failed", inbound_id=inbound_id, state=state,
                      attempts=attempts, error=str(exc))
                if verbose:
                    traceback.print_exc()
                run["state"] = "FAILED"
                break
            update_run(conn, inbound_id, attempt_count=attempts, last_error=str(exc))
            run["attempt_count"] = attempts
            event(_log, "retry", inbound_id=inbound_id, state=state,
                  attempt=attempts, error=str(exc))
            continue

        _assert_legal(state, next_state)
        dur = int((time.monotonic() - t0) * 1000)
        persist = {k: v for k, v in patch.items() if not k.startswith("_")}
        update_run(conn, inbound_id, state=next_state, last_error=None, **persist)
        event(_log, "transition", inbound_id=inbound_id, **{"from": state},
              to=next_state, duration_ms=dur)
        run.update(patch)
        run["state"] = next_state

    llm.set_recorder(None)
    set_event_sink(None)
    final = get_run(conn, inbound_id)
    if verbose:
        print(f"  [{inbound_id}] -> {final['state']} (tier={final.get('final_tier')})")
    return final


def _assert_legal(frm: str, to: str) -> None:
    if to not in LEGAL.get(frm, set()):
        raise RuntimeError(f"illegal transition {frm} -> {to}")


# --------------------------------------------------------------------------- #
# Operator re-open path (blueprint §2.1)                                       #
# --------------------------------------------------------------------------- #

_REOPEN_TARGET = {"HELD": "COMPOSED", "FAILED": "ENRICHED"}


def reopen(conn, inbound_id: int) -> dict:
    """Re-arm a parked run. Legal only from HELD/FAILED. Records an operator
    audit row and resets the lifetime attempt budget so the human's decision to
    retry is explicit and traceable."""
    run = get_run(conn, inbound_id)
    if run is None:
        raise KeyError(f"no run for inbound_id={inbound_id}")
    if run["state"] not in _REOPEN_TARGET:
        raise ValueError(f"run {inbound_id} is {run['state']}, not reopenable "
                         f"(only HELD/FAILED)")
    target = _REOPEN_TARGET[run["state"]]
    record_decision(conn, inbound_id, "operator", f"reopen->{target}",
                    f"operator re-armed run from {run['state']}", "operator",
                    json.dumps({"from": run["state"], "to": target}))
    update_run(conn, inbound_id, state=target, attempt_count=0, last_error=None)
    event(_log, "reopened", inbound_id=inbound_id, **{"from": run["state"]}, to=target)
    return get_run(conn, inbound_id)


# --------------------------------------------------------------------------- #
# Small helpers                                                                #
# --------------------------------------------------------------------------- #

def _latest_decision(conn, inbound_id: int, stage: str) -> dict:
    d = get_decision(conn, inbound_id, stage)
    return json.loads(d["raw_json"]) if d else {}


def _latest_draft(conn, inbound_id: int) -> dict:
    """Re-derive the approved draft from the stored approve decision (crash resume)."""
    d = _latest_decision(conn, inbound_id, "approve")
    return d.get("draft", {"channels": []})


# Recommended operator next step per tier — shown on the Slack card and in the
# review queue so the human always knows WHAT the pipeline wants to happen next.
NEXT_STEP = {
    "Hot":  "Approve the draft now and push for a call this week — high-touch, "
            "respond within the hour.",
    "Warm": "Approve the draft on today's review pass — standard outreach, one "
            "clear CTA.",
    "Mild": "Approve when convenient — light nurture only, no call push.",
}


def _payload_for(channel: str, ctx: LeadContext, tier: str, draft: dict,
                 conn=None) -> dict:
    if channel == "slack":
        # The Slack review card — the human approve surface. It must answer, on
        # its own, the three questions an SDR manager will actually ask:
        # WHY this tier (qualifier evidence), WAS IT CHECKED (judge verdict),
        # and WHAT NEXT (recommended action + how to approve).
        lines = [
            f"*Tier:* {tier}  ·  *Topic:* {draft.get('topic', '?')}"
            f"  ·  *Source:* {ctx.source}",
            f"*Asked:* {ctx.request_for[:280]}",
        ]
        firmo = []
        if ctx.current_provider:
            firmo.append(f"currently on {ctx.current_provider}")
        if ctx.monthly_volume:
            firmo.append(f"volume {ctx.monthly_volume}")
        if ctx.industry:
            firmo.append(ctx.industry)
        if ctx.company_employees:
            firmo.append(f"{ctx.company_employees} employees")
        if firmo:
            lines.append("*Firmographics:* " + " · ".join(firmo))
        if conn is not None:
            qv = _latest_decision(conn, ctx.inbound_id, "qualify")
            if qv:
                lines.append(
                    f"*Why {tier}:* icp_fit {qv.get('icp_fit', '?')} · "
                    f"intent {qv.get('intent', '?')} · "
                    + "; ".join(qv.get("signals", [])[:4])
                )
            jv = _latest_decision(conn, ctx.inbound_id, "judge")
            if jv:
                jline = (f"*Judge ({'agrees' if jv.get('agree') else 'overrode'}):* "
                         f"{jv.get('reason', '')[:280]}")
                if jv.get("hallucination_flags"):
                    jline += f"  :warning: flags: {'; '.join(jv['hallucination_flags'][:3])}"
                lines.append(jline)
        lines.append(f"*Draft ready for review:* “{draft.get('subject', '')}”")
        lines.append(f"*Next step:* {NEXT_STEP.get(tier, 'review manually')}")
        lines.append(
            f":white_check_mark: type `approve` in the review console "
            f"(`py -m quinn.run --review`) to create the Gmail draft — "
            f"or `py -m quinn.run --approve-mail {ctx.inbound_id}` / "
            f"`--reject-mail {ctx.inbound_id}`"
        )
        return {
            "to": "#all-telnyx-agent",
            "subject": f"[{tier}] {ctx.company} — {ctx.name} ({ctx.role})",
            "text": "\n".join(lines),
        }
    return {
        "to": ctx.email,
        "subject": draft.get("subject", ""),
        "body": draft.get("body", ""),
    }


# --------------------------------------------------------------------------- #
# Convenience entrypoints (thin; run.py is the real CLI)                      #
# --------------------------------------------------------------------------- #

def run_all(conn=None, *, verbose: bool = True) -> list[dict]:
    own = conn is None
    conn = conn or get_connection()
    try:
        init_db(conn)
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM inbound_requests ORDER BY id").fetchall()]
        return [run_lead(conn, i, verbose=verbose) for i in ids]
    finally:
        if own:
            conn.close()
