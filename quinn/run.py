"""Quinn orchestration CLI.

    py -m quinn.run --inbound-id 5    # drive one lead through the FSM
    py -m quinn.run --all             # process the whole inbound queue
    py -m quinn.run --resume          # re-drive any run still in a non-terminal state
    py -m quinn.run --review          # INTERACTIVE approval console (the human gate)
    py -m quinn.run --status          # per-run state, tier, LLM calls, tokens, latency
    py -m quinn.run --trace 1         # full life of one lead from persisted state
    py -m quinn.run --costs           # token/call spend per stage across the queue
    py -m quinn.run --reopen 7        # operator: re-arm a HELD/FAILED run
    py -m quinn.run --approve-mail 1  # human step 1: approve -> create Gmail draft
    py -m quinn.run --send-mail 1     # human step 2: send the checked Gmail draft
    py -m quinn.run --reject-mail 1   # human: reject -> no draft / discard draft
    py -m quinn.gmail                 # one-time Gmail OAuth consent
    py -m quinn.web                   # CRM dashboard (http://localhost:8642)

The workflow around the human (nothing outbound is ever autonomous):
    pipeline -> Slack review card (why the tier + judge check + next step)
             -> operator types `approve` in --review        (step 1)
             -> Gmail DRAFT created, "check your Drafts" prompt
             -> only then does the console move to the next lead
             -> --send-mail fires the actual send            (step 2)

All driving commands are safe to run repeatedly — idempotency guarantees no
duplicate outreach and the resume-skip guard guarantees no duplicate LLM spend.
Demo money-shot: run `--all` twice → 0 additional sends, 0 additional LLM calls.
"""

from __future__ import annotations

import argparse
import functools
import json

import quinn.config  # noqa: F401 — MUST precede other quinn imports: loads .env so
#                      integrations picks up live creds (Slack webhook) at import.
from quinn.agent import NEXT_STEP, TERMINAL, human_rewrite, reopen, run_lead
from quinn.db import get_connection, init_db
from quinn.integrations import (
    human_approve,
    human_reject,
    human_send,
    human_unreject,
    post_run_summary,
)
from quinn.obs import set_event_sink
from quinn.repo import record_event


# Prints the state of every lead plus the two "waiting on a human" queues.
def cmd_status(conn) -> None:
    runs = conn.execute("SELECT * FROM lead_runs ORDER BY inbound_id").fetchall()
    if not runs:
        print("no runs yet — try --all")
        return
    print(f"{'lead':>4}  {'state':<11} {'tier':<5} {'att':>3} "
          f"{'calls':>5} {'tokens':>7} {'ms':>7}  last_error")
    print("-" * 72)
    for r in runs:
        iid = r["inbound_id"]
        agg = conn.execute(
            "SELECT COUNT(*) c, "
            "COALESCE(SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)),0) tok, "
            "COALESCE(SUM(latency_ms),0) ms FROM llm_calls WHERE inbound_id=?",
            (iid,),
        ).fetchone()
        print(f"{iid:>4}  {r['state']:<11} {(r['final_tier'] or '-'):<5} "
              f"{r['attempt_count']:>3} {agg['c']:>5} {agg['tok']:>7} {agg['ms']:>7}  "
              f"{(r['last_error'] or '')[:26]}")
    sent = conn.execute("SELECT COUNT(*) c FROM outbox WHERE status='sent'").fetchone()["c"]
    calls = conn.execute("SELECT COUNT(*) c FROM llm_calls").fetchone()["c"]
    print("-" * 72)
    print(f"outbox: {sent} messages sent (exactly-once) · {calls} total LLM calls")
    # The operator's two work queues (the two steps of the human gate):
    #   1. review card posted, no draft yet  -> awaiting `approve` (--review)
    #   2. draft created, not yet sent       -> awaiting check + --send-mail
    awaiting = _awaiting_approval(conn)
    if awaiting:
        ids = ", ".join(str(r["inbound_id"]) for r in awaiting)
        print(f"awaiting approval: {len(awaiting)} (leads {ids}) — run --review")
    drafted = conn.execute(
        "SELECT o.inbound_id FROM outbox o WHERE o.channel='email' "
        "AND o.idempotency_key LIKE 'deliver:%' AND o.status='sent' "
        "AND NOT EXISTS (SELECT 1 FROM outbox s "
        "                WHERE s.idempotency_key = 'send:' || o.inbound_id || ':email')"
    ).fetchall()
    if drafted:
        ids = ", ".join(str(r["inbound_id"]) for r in drafted)
        print(f"drafts awaiting final send: {len(drafted)} (leads {ids}) — "
              f"check Gmail Drafts, then --send-mail ID / --reject-mail ID")


# Prints one lead's whole life story — every decision, AI call, and delivery
# in time order, rebuilt purely from what's saved in the database.
def cmd_trace(conn, inbound_id: int) -> None:
    """Chronological merge of decisions + llm_calls + outbox for one lead."""
    run = conn.execute("SELECT * FROM lead_runs WHERE inbound_id=?", (inbound_id,)).fetchone()
    if run is None:
        print(f"no run for lead {inbound_id}")
        return
    lead = conn.execute("SELECT name, company FROM inbound_requests WHERE id=?",
                        (inbound_id,)).fetchone()
    print(f"=== TRACE lead {inbound_id}: {lead['name']} @ {lead['company']} ===")
    print(f"final state: {run['state']}   tier: {run['final_tier']}   "
          f"attempts: {run['attempt_count']}")

    events = []
    for d in conn.execute("SELECT * FROM decisions WHERE inbound_id=? ORDER BY id", (inbound_id,)):
        events.append((d["created_at"], "decision",
                       f"{d['stage']:<9} -> {d['verdict']:<12} [{d['model']}]  {d['rationale'][:70]}"))
    for c in conn.execute("SELECT * FROM llm_calls WHERE inbound_id=? ORDER BY id", (inbound_id,)):
        tok = (c["prompt_tokens"] or 0) + (c["completion_tokens"] or 0)
        events.append((c["created_at"], "llm_call",
                       f"{c['stage'] or c['task']:<9} {c['used_model'] or c['requested_model']} "
                       f"att{c['attempt']} {c['outcome']} {tok}tok {c['latency_ms']}ms"))
    for o in conn.execute("SELECT * FROM outbox WHERE inbound_id=? ORDER BY id", (inbound_id,)):
        events.append((o["claimed_at"], "delivery",
                       f"{o['channel']:<9} {o['status']}  key={o['idempotency_key']}"))
    for ts, kind, line in sorted(events, key=lambda e: e[0]):
        print(f"  {ts}  {kind:<9} {line}")


# Prints how many AI calls and tokens each pipeline stage has spent.
def cmd_costs(conn) -> None:
    print(f"{'stage':<10} {'calls':>6} {'prompt_tok':>11} {'compl_tok':>10} {'ms':>8}")
    print("-" * 50)
    rows = conn.execute(
        "SELECT COALESCE(stage,task) stage, COUNT(*) c, "
        "COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) k, "
        "COALESCE(SUM(latency_ms),0) ms FROM llm_calls GROUP BY COALESCE(stage,task) "
        "ORDER BY p DESC"
    ).fetchall()
    for r in rows:
        print(f"{r['stage']:<10} {r['c']:>6} {r['p']:>11} {r['k']:>10} {r['ms']:>8}")
    tot = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(prompt_tokens),0) p, "
                       "COALESCE(SUM(completion_tokens),0) k FROM llm_calls").fetchone()
    print("-" * 50)
    print(f"{'TOTAL':<10} {tot['c']:>6} {tot['p']:>11} {tot['k']:>10}")


# --------------------------------------------------------------------------- #
# Interactive review console — the human half of the workflow                  #
# --------------------------------------------------------------------------- #

# Finds the leads whose review card is up but no human has decided yet —
# the review console's work queue, hottest first.
def _awaiting_approval(conn) -> list:
    """Leads whose Slack review card is posted (state DONE) but which the human
    has not yet approved (no deliver:{id}:email draft) or rejected (no
    send:{id}:email verdict). Hot first — that's the queue's whole point."""
    return conn.execute(
        "SELECT lr.inbound_id, lr.final_tier FROM lead_runs lr "
        "WHERE lr.state='DONE' "
        "AND NOT EXISTS (SELECT 1 FROM outbox o "
        "                WHERE o.idempotency_key='deliver:'||lr.inbound_id||':email') "
        "AND NOT EXISTS (SELECT 1 FROM outbox s "
        "                WHERE s.idempotency_key='send:'||lr.inbound_id||':email') "
        "ORDER BY CASE lr.final_tier WHEN 'Hot' THEN 0 WHEN 'Warm' THEN 1 "
        "ELSE 2 END, lr.inbound_id"
    ).fetchall()


# Fetches one saved verdict's details as a plain dict (empty if none yet).
def _decision_doc(conn, inbound_id: int, stage: str) -> dict:
    row = conn.execute(
        "SELECT raw_json FROM decisions WHERE inbound_id=? AND stage=? "
        "ORDER BY id DESC LIMIT 1", (inbound_id, stage)).fetchone()
    return json.loads(row["raw_json"]) if row else {}


# Prints everything a reviewer needs for one lead in the terminal: who they
# are, why they got their tier, what the judge said, and the draft itself.
def _print_review_card(conn, iid: int, pos: int, total: int) -> None:
    """Terminal mirror of the Slack card: everything needed to decide, inline."""
    lead = conn.execute("SELECT * FROM inbound_requests WHERE id=?", (iid,)).fetchone()
    run = conn.execute("SELECT * FROM lead_runs WHERE inbound_id=?", (iid,)).fetchone()
    q = _decision_doc(conn, iid, "qualify")
    j = _decision_doc(conn, iid, "judge")
    draft = _decision_doc(conn, iid, "approve").get("draft", {})
    print("\n" + "=" * 72)
    print(f"[{pos}/{total}] Lead {iid} — {lead['name']} ({lead['role']}) "
          f"@ {lead['company']}   tier: {run['final_tier']}")
    print(f"  asked     : {lead['request_for'][:200]}")
    if q:
        print(f"  qualifier : icp_fit={q.get('icp_fit')} intent={q.get('intent')} "
              f"topic={q.get('primary_topic')}")
        for s in q.get("signals", [])[:4]:
            print(f"              + {s}")
    if j:
        print(f"  judge     : {'agrees' if j.get('agree') else 'OVERRODE'} — "
              f"{j.get('reason', '')[:200]}")
        for f in j.get("hallucination_flags", [])[:3]:
            print(f"              ! flag: {f}")
    print(f"  next step : {NEXT_STEP.get(run['final_tier'], 'review manually')}")
    print(f"  draft     : \"{draft.get('subject', '')}\"")
    for line in (draft.get("body", "") or "").splitlines()[:14]:
        print(f"      | {line}")
    print("=" * 72)


# The interactive approval console: shows one lead at a time and waits for
# you to type approve / rewrite / reject / skip. Approve creates the Gmail
# draft and pauses until you've checked it — then moves to the next lead.
def cmd_review(conn) -> None:
    """One lead at a time: show the full why, wait for the operator's verdict.

    `approve` creates the Gmail draft for THAT lead and pauses until the
    operator confirms they've checked it in Gmail Drafts — only then does the
    queue advance. This is deliberately sequential: outreach review deserves
    full attention per lead, not a bulk rubber stamp.
    """
    rows = _awaiting_approval(conn)
    if not rows:
        print("review queue is empty — nothing awaiting approval.")
        return
    print(f"REVIEW QUEUE: {len(rows)} lead(s) awaiting approval (Hot first).")
    for pos, r in enumerate(rows, 1):
        iid = r["inbound_id"]
        _print_review_card(conn, iid, pos, len(rows))
        while True:
            ans = input("  approve / rewrite / reject / skip / quit > ").strip().lower()
            if ans in ("approve", "a"):
                status = human_approve(conn, iid)
                if status == "drafted":
                    print("  [ok] Gmail draft created (Slack notified).")
                    input("  Check the draft in Gmail -> Drafts, then press "
                          "Enter to continue to the next lead... ")
                else:
                    print(f"  -> {status}")
                break
            if ans in ("rewrite", "w"):
                fb = input("  feedback for the new draft (optional) > ").strip()
                res = human_rewrite(conn, iid, fb)
                print(f"  -> new draft: \"{res['subject']}\" "
                      f"({'passed the gate' if res['approved'] else 'BLOCKED: ' + res['reason'][:80]})")
                _print_review_card(conn, iid, pos, len(rows))   # show the new draft
                continue                     # same lead: approve/reject the rewrite
            if ans in ("reject", "r"):
                reason = input("  reason (optional) > ").strip() \
                    or "rejected by operator"
                print(f"  -> {human_reject(conn, iid, reason)} "
                      "(undo later: --unreject or the Rejected tab)")
                break
            if ans in ("skip", "s"):
                break
            if ans in ("quit", "q"):
                print("review ended — remaining leads stay in the queue.")
                return
    print("\nreview pass complete.")
    cmd_status(conn)


# Processes every lead in the queue, then posts the Slack summary.
def cmd_all(conn, verbose: bool) -> None:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM inbound_requests ORDER BY id").fetchall()]
    print(f"processing {len(ids)} inbound leads...")
    for i in ids:
        run_lead(conn, i, verbose=verbose)
    if post_run_summary(conn):
        print("posted run summary to Slack (#all-telnyx-agent)")


# Picks up every lead that got interrupted mid-pipeline and finishes it.
# Finished steps are skipped, so resuming costs almost nothing.
def cmd_resume(conn, verbose: bool) -> None:
    placeholders = ",".join("?" for _ in TERMINAL)
    rows = conn.execute(
        f"SELECT inbound_id FROM lead_runs WHERE state NOT IN ({placeholders})",
        tuple(TERMINAL),
    ).fetchall()
    if not rows:
        print("nothing to resume — all runs terminal")
        return
    print(f"resuming {len(rows)} non-terminal run(s)...")
    for r in rows:
        run_lead(conn, r["inbound_id"], verbose=verbose)


# Reads the command-line flags and calls the matching command above.
def main() -> None:
    p = argparse.ArgumentParser(prog="quinn.run", description="Quinn inbound orchestrator")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--inbound-id", type=int, help="drive a single lead")
    g.add_argument("--all", action="store_true", help="process the whole queue")
    g.add_argument("--resume", action="store_true", help="re-drive non-terminal runs")
    g.add_argument("--status", action="store_true", help="print run states + telemetry")
    g.add_argument("--trace", type=int, metavar="ID", help="full trace of one lead")
    g.add_argument("--costs", action="store_true", help="token/call spend per stage")
    g.add_argument("--reopen", type=int, metavar="ID", help="re-arm a HELD/FAILED run")
    g.add_argument("--review", action="store_true",
                   help="interactive approval console (approve/reject per lead)")
    g.add_argument("--approve-mail", type=int, metavar="ID",
                   help="human step 1: approve -> create the Gmail draft")
    g.add_argument("--send-mail", type=int, metavar="ID",
                   help="human step 2: send the checked Gmail draft")
    g.add_argument("--reject-mail", type=int, metavar="ID",
                   help="human rejection: discard the lead's Gmail draft")
    g.add_argument("--rewrite", type=int, metavar="ID",
                   help="human: recompose the lead's draft (prompts for feedback)")
    g.add_argument("--unreject", type=int, metavar="ID",
                   help="human: undo a rejection — lead returns to the review queue")
    p.add_argument("--quiet", action="store_true", help="less per-state logging")
    args = p.parse_args()

    conn = get_connection()
    try:
        init_db(conn)
        # Persist events for HUMAN actions too (approve/reject/send from this
        # CLI), not only pipeline runs — the observability story has no gaps.
        set_event_sink(functools.partial(record_event, conn))
        verbose = not args.quiet
        if args.status:
            cmd_status(conn)
        elif args.trace is not None:
            cmd_trace(conn, args.trace)
        elif args.costs:
            cmd_costs(conn)
        elif args.reopen is not None:
            reopen(conn, args.reopen)
            final = run_lead(conn, args.reopen, verbose=verbose)  # re-drive now
            print(f"re-armed lead {args.reopen} -> {final['state']} "
                  f"(tier={final.get('final_tier')})")
        elif args.review:
            cmd_review(conn)
        elif args.approve_mail is not None:
            status = human_approve(conn, args.approve_mail)
            print(f"lead {args.approve_mail} email: {status}"
                  + (" — check Gmail Drafts, then --send-mail"
                     if status == "drafted" else ""))
        elif args.send_mail is not None:
            status = human_send(conn, args.send_mail)
            print(f"lead {args.send_mail} email: {status}")
        elif args.reject_mail is not None:
            status = human_reject(conn, args.reject_mail)
            print(f"lead {args.reject_mail} email: {status}")
        elif args.rewrite is not None:
            fb = input("feedback for the new draft (optional) > ").strip()
            res = human_rewrite(conn, args.rewrite, fb)
            print(f"lead {args.rewrite} new draft: \"{res['subject']}\" "
                  f"({'passed the gate' if res['approved'] else 'BLOCKED: ' + res['reason'][:100]})")
        elif args.unreject is not None:
            status = human_unreject(conn, args.unreject)
            print(f"lead {args.unreject}: {status}")
        elif args.all:
            cmd_all(conn, verbose)
            cmd_status(conn)
        elif args.resume:
            cmd_resume(conn, verbose)
            cmd_status(conn)
        else:
            run_lead(conn, args.inbound_id, verbose=verbose)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
