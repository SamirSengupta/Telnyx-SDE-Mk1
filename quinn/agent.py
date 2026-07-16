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

from quinn import approver, email_composer, integrations, judge, knowledge, llm, repo
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


# The first-pass sorter. Shows the lead to a fast model and asks "Hot, Warm,
# Mild, or Cold — and what are they asking about?". If the model can't be
# reached at all, it plays safe and answers Cold (don't contact).
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

# Room 1: the lead just arrived. Its data was already fetched, so this step
# simply moves it forward.
def _h_received(conn, ctx, run):
    # ENRICH: context is already assembled by the driver; nothing else to do.
    return "ENRICHED", {}


# Room 2: run the qualifier — unless a saved verdict already exists (from an
# earlier run), in which case reuse it and spend nothing.
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


# Room 3: the judge double-checks the qualifier's tier. Whatever the judge
# says becomes the final tier. Same reuse rule as above on re-runs.
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


# Room 4: the fork. Cold leads exit here for good (SUPPRESSED — never
# contacted). Everyone else moves on to get an email written.
def _h_judged(conn, ctx, run):
    tier = run["final_tier"]
    if tier == "Cold":
        event(_log, "suppressed", inbound_id=ctx.inbound_id, tier=tier)
        return "SUPPRESSED", {}
    return "COMPOSED", {}


# Room 5: fetch the verified Telnyx facts, have the writer draft the email
# grounded in them, then run the draft through the approval gate. Pass ->
# onward. Blocked -> the lead parks in HELD for a human. On re-runs the saved
# outcome is reused — except right after a human clicked Re-arm, which forces
# a fresh compose.
def _h_composed(conn, ctx, run):
    tier = run["final_tier"]
    prior = get_decision(conn, ctx.inbound_id, "approve")
    # Resume-skip guard — BUT an operator Re-arm (reopen) that happened AFTER the
    # last approve decision means the human explicitly asked to retry, so we must
    # NOT reuse that (blocked) decision; we recompose fresh below. Without this,
    # Re-arm would just re-read the old block and bounce the lead back to HELD.
    op = get_decision(conn, ctx.inbound_id, "operator")
    rearmed = op is not None and prior is not None and op["id"] > prior["id"]
    if prior is not None and not rearmed:
        payload = json.loads(prior["raw_json"])
        event(_log, "decision_reused", inbound_id=ctx.inbound_id, stage="approve")
        if prior["verdict"] != "pass":
            return "HELD", {}
        run["_draft"] = payload["draft"]
        return "APPROVED", {}

    # Topic-routed template: the qualifier classified WHY they reached out.
    topic = _latest_decision(conn, ctx.inbound_id, "qualify") \
        .get("primary_topic", "platform_other")
    # GROUNDING: pull verified Telnyx facts for this lead's topic + ask, so the
    # composer writes and the approver checks against a citable fact base rather
    # than the model's parametric memory. Recorded on the decision for audit.
    facts = knowledge.retrieve_facts(conn, topic, ctx)
    facts_block = knowledge.format_facts(facts)
    event(_log, "tool_call", inbound_id=ctx.inbound_id, tool="retrieve_facts",
          topic=topic, facts=len(facts))
    draft, cmodel = email_composer.compose(ctx, tier, topic, facts_block)
    verdict, amodel = approver.approve(conn, ctx, tier, draft, facts_block)
    draft_record = {**draft.model_dump(), "topic": topic,
                    "channels": email_composer.channels_for(tier)}
    record_decision(conn, ctx.inbound_id, "approve",
                    "pass" if verdict.approved else "block",
                    verdict.reason, amodel,
                    json.dumps({"approval": verdict.model_dump(),
                                "draft": draft_record, "compose_model": cmodel,
                                "grounding_facts": knowledge.fact_refs(facts)}))
    event(_log, "decision", inbound_id=ctx.inbound_id, stage="approve",
          verdict="pass" if verdict.approved else "block", model=amodel)
    if not verdict.approved:
        return "HELD", {}
    run["_draft"] = draft_record
    return "APPROVED", {}


# Room 6: post the review card to Slack (exactly once) and stop. The machine's
# work ends here — no email exists yet; that takes a human clicking approve.
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


# Room 7: card is up — mark the automated journey DONE (= "awaiting human").
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

# The engine. Walks one lead room by room until it reaches an end state,
# saving progress after every step. Errors get a bounded number of retries
# (3 per lead, total) before the lead parks in FAILED for a human. Safe to
# call again anytime — finished work is never redone or re-billed.
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
    # Token bucketing: let llm.complete() read cumulative per-model spend so it
    # can skip a model whose 100k bucket is full and fail over to its backup.
    llm.set_usage_provider(functools.partial(repo.model_tokens_used, conn))

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
    llm.set_usage_provider(None)
    final = get_run(conn, inbound_id)
    if verbose:
        print(f"  [{inbound_id}] -> {final['state']} (tier={final.get('final_tier')})")
    return final


# The bouncer. Refuses any state jump that isn't on the LEGAL list, so even a
# buggy handler can't push a lead somewhere it shouldn't go.
def _assert_legal(frm: str, to: str) -> None:
    if to not in LEGAL.get(frm, set()):
        raise RuntimeError(f"illegal transition {frm} -> {to}")


# --------------------------------------------------------------------------- #
# Operator re-open path (blueprint §2.1)                                       #
# --------------------------------------------------------------------------- #

_REOPEN_TARGET = {"HELD": "COMPOSED", "FAILED": "ENRICHED"}


# The Re-arm button. Takes a parked lead (HELD or FAILED), resets its retry
# budget, and puts it back in the pipeline so the next run tries it fresh.
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


# The Rewrite button. Throws away the current draft (and its Gmail copy, if
# one exists), writes a new one guided by the operator's feedback, re-runs the
# approval gate, and saves the result as a new decision. The old draft stays
# in the history — nothing is ever erased from the audit trail.
def human_rewrite(conn, inbound_id: int, feedback: str = "") -> dict:
    """Operator asks for a NEW draft (with optional feedback) before approving.

    Third option beside approve/reject: recompose the email — showing the model
    the previous draft plus the operator's notes — re-run the approval gate,
    and append a NEW approve decision (append-only: the old draft stays in the
    audit trail; the latest decision is what approve/draft flows read). Any
    existing Gmail draft is discarded first so stale content can never be the
    thing that gets sent. Not allowed once sent; a rejected lead must be
    un-rejected first."""
    setup_logging()
    llm.set_recorder(functools.partial(record_llm_call, conn))
    set_event_sink(functools.partial(record_event, conn))
    llm.set_usage_provider(functools.partial(repo.model_tokens_used, conn))
    try:
        run = get_run(conn, inbound_id)
        if run is None or run.get("final_tier") not in ("Hot", "Warm", "Mild"):
            raise ValueError(f"lead {inbound_id} has no outreach-tier run to rewrite")
        srow = conn.execute("SELECT status FROM outbox WHERE idempotency_key=?",
                            (f"send:{inbound_id}:email",)).fetchone()
        if srow and srow["status"] == "sent":
            raise RuntimeError(f"lead {inbound_id}'s email was already sent — "
                               "nothing to rewrite")
        if srow and srow["status"] == "rejected":
            raise RuntimeError(f"lead {inbound_id} is rejected — undo the "
                               "reject first (Rejected tab / --unreject)")

        ctx = load_lead_context(conn, inbound_id)
        tier = run["final_tier"]
        topic = _latest_decision(conn, inbound_id, "qualify") \
            .get("primary_topic", "platform_other")
        previous = _latest_draft(conn, inbound_id)

        # Old Gmail draft (if approved already) must die BEFORE the new pass —
        # both so the approver's prior-send policy check doesn't fire and so a
        # stale draft can never linger next to the new content.
        integrations.discard_draft(conn, inbound_id)

        facts = knowledge.retrieve_facts(conn, topic, ctx)
        facts_block = knowledge.format_facts(facts)
        event(_log, "tool_call", inbound_id=inbound_id, tool="retrieve_facts",
              topic=topic, facts=len(facts))
        draft, cmodel = email_composer.compose(ctx, tier, topic, facts_block,
                                               feedback=feedback,
                                               previous=previous)
        verdict, amodel = approver.approve(conn, ctx, tier, draft, facts_block)
        draft_record = {**draft.model_dump(), "topic": topic,
                        "channels": email_composer.channels_for(tier)}
        record_decision(conn, inbound_id, "approve",
                        "pass" if verdict.approved else "block",
                        verdict.reason, amodel,
                        json.dumps({"approval": verdict.model_dump(),
                                    "draft": draft_record,
                                    "compose_model": cmodel,
                                    "grounding_facts": knowledge.fact_refs(facts),
                                    "rewrite": True, "feedback": feedback}))
        event(_log, "human_rewrite", inbound_id=inbound_id,
              approved=verdict.approved, feedback_len=len(feedback))
        return {"approved": verdict.approved, "subject": draft.subject,
                "reason": verdict.reason}
    finally:
        llm.set_recorder(None)
        set_event_sink(None)
        llm.set_usage_provider(None)


# --------------------------------------------------------------------------- #
# Small helpers                                                                #
# --------------------------------------------------------------------------- #

# Grabs the newest saved verdict for one step and unpacks its JSON details.
def _latest_decision(conn, inbound_id: int, stage: str) -> dict:
    d = get_decision(conn, inbound_id, stage)
    return json.loads(d["raw_json"]) if d else {}


# Recovers the saved email draft from the decision log (used after a crash,
# when the in-memory copy is gone).
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


# Builds what actually gets sent on a channel: for Slack, the rich review card
# (who, why this tier, what the judge said, next step); for email, the
# to/subject/body of the draft.
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

# Runs every lead in the inbound queue through the pipeline, one at a time.
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
