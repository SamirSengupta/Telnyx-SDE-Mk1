"""Pipeline tests — the headline guarantees, exercised end-to-end.

The fake is injected at ONE seam: ``llm._post_chat`` (the transport). Everything
above it — routing, pydantic validation, telemetry recording, the FSM, the
outbox protocol — runs for real. That's deliberate: the tests cover the code
that matters, not a hollowed-out mock of it.

Run directly (no pytest needed):   py -m tests.test_pipeline
Or under pytest:                    pytest tests/
"""

from __future__ import annotations

import json
import os
import tempfile

from quinn import integrations, llm
from quinn.db import get_connection, init_db
from quinn.integrations import DeliveryReceipt
from quinn.seed import seed


# --------------------------------------------------------------------------- #
# Fake proxy transport — returns OpenAI-shaped responses by inspecting the     #
# system prompt to tell which stage is calling.                               #
# --------------------------------------------------------------------------- #

def _fake_post(body: dict) -> dict:
    sys = body["messages"][0]["content"]
    usr = body["messages"][1]["content"]
    if "inbound SDR qualifier" in sys:
        tier = "Cold" if ("student" in usr.lower() or "diligence" in usr.lower()) else "Hot"
        # primary_topic included for lead-like prompts; TierVerdict must also
        # default it when absent (backward compat is covered by the judge fake
        # and older recorded decisions).
        content = {"tier": tier, "primary_topic": "messaging", "icp_fit": 0.9,
                   "intent": 0.85, "signals": ["fit"], "disqualifiers": [],
                   "rationale": "looks strong"}
    elif "QA judge" in sys:
        tier = "Cold" if "tier: Cold" in usr else "Hot"
        content = {"agree": True, "final_tier": tier, "changed": False,
                   "hallucination_flags": [], "reason": "confirmed"}
    elif "outbound sales emails" in sys:
        content = {"subject": "Cutting your messaging spend at Telnyx",
                   "body": ("Hi — saw you're scaling messaging and looking to cut "
                            "provider costs while improving deliverability. Happy to "
                            "walk through options. Best, Quinn — Telnyx"),
                   "grounded_facts": ["volume", "provider"]}
    elif "approval gate" in sys:
        content = {"approved": True, "issues": [], "reason": "truthful and on-tier"}
    else:
        raise AssertionError("unknown stage in system prompt")
    return {"choices": [{"message": {"content": json.dumps(content)}}],
            "model": "fake-" + body["model"],
            "usage": {"prompt_tokens": 120, "completion_tokens": 40}}


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    conn = get_connection(path)
    init_db(conn)
    seed(conn)
    return conn


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

def test_idempotency_and_no_double_spend():
    """Run the whole queue twice: 0 extra sends AND 0 extra LLM calls."""
    llm._post_chat = _fake_post
    from quinn import agent
    conn = _fresh_db()

    agent.run_all(conn, verbose=False)
    sent1 = conn.execute("SELECT COUNT(*) c FROM outbox WHERE status='sent'").fetchone()["c"]
    calls1 = conn.execute("SELECT COUNT(*) c FROM llm_calls").fetchone()["c"]

    agent.run_all(conn, verbose=False)
    sent2 = conn.execute("SELECT COUNT(*) c FROM outbox WHERE status='sent'").fetchone()["c"]
    calls2 = conn.execute("SELECT COUNT(*) c FROM llm_calls").fetchone()["c"]

    assert sent1 == sent2, f"double-send: {sent1} -> {sent2}"
    assert calls1 == calls2, f"double-spend: {calls1} -> {calls2}"
    dups = conn.execute("SELECT idempotency_key FROM outbox GROUP BY idempotency_key "
                        "HAVING COUNT(*)>1").fetchall()
    assert not dups, "duplicate outbox keys"
    assert calls1 > 0 and sent1 > 0
    # Observability contract: every LLM row carries its full prompts/response,
    # and the FSM's every step landed in the persisted event stream.
    unlogged = conn.execute("SELECT COUNT(*) c FROM llm_calls "
                            "WHERE user_prompt IS NULL OR system_prompt IS NULL"
                            ).fetchone()["c"]
    assert unlogged == 0, "llm_calls missing prompt capture"
    n_events = conn.execute("SELECT COUNT(*) c FROM pipeline_events").fetchone()["c"]
    n_transitions = conn.execute("SELECT COUNT(*) c FROM pipeline_events "
                                 "WHERE name='transition'").fetchone()["c"]
    assert n_events > 0 and n_transitions > 0, "pipeline_events not persisted"
    print(f"  idempotency OK — sent={sent1} (stable), llm_calls={calls1} (stable), "
          f"events={n_events} persisted with full prompts")


def test_suppression_blocks_send():
    """A lead on the suppression list is parked in HELD, never emailed."""
    llm._post_chat = _fake_post
    from quinn import agent
    conn = _fresh_db()
    agent.run_all(conn, verbose=False)

    held = conn.execute("SELECT ir.email FROM lead_runs lr "
                        "JOIN inbound_requests ir ON ir.id=lr.inbound_id "
                        "WHERE lr.state='HELD'").fetchall()
    assert any(r["email"].endswith("@souqexpress.ae") for r in held), \
        "suppressed lead should be HELD"
    sent_to_suppressed = conn.execute(
        "SELECT COUNT(*) c FROM outbox o JOIN inbound_requests ir ON ir.id=o.inbound_id "
        "WHERE ir.email LIKE '%@souqexpress.ae' AND o.channel='email'").fetchone()["c"]
    assert sent_to_suppressed == 0, "suppressed recipient must never be emailed"
    print("  suppression OK — opted-out lead parked in HELD, no email")


def test_reconcile_after_delivery_failure():
    """A delivery fails once, then succeeds on retry — no duplicate, correct end
    state. Covers both sides of the human gate: the pipeline's Slack card and
    the human-approve Gmail draft."""
    llm._post_chat = _fake_post
    from quinn import agent
    from quinn.integrations import human_approve

    # 1. Flaky SLACK during the pipeline: the FSM must retry the DELIVER state
    #    and end DONE with exactly one review card.
    calls = {"n": 0}
    class FlakySlack:
        def send(self, *, idempotency_key, payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated webhook blip")
            return DeliveryReceipt(provider_msg_id="ok_" + idempotency_key, channel="slack")
    orig_slack = integrations.NOTIFIERS["slack"]
    integrations.NOTIFIERS["slack"] = FlakySlack()
    try:
        conn = _fresh_db()
        agent.run_lead(conn, 1, verbose=False)   # a Hot, non-suppressed lead
        run = conn.execute("SELECT * FROM lead_runs WHERE inbound_id=1").fetchone()
        cards = conn.execute("SELECT COUNT(*) c FROM outbox WHERE inbound_id=1 "
                             "AND channel='slack' AND status='sent'").fetchone()["c"]
        assert run["state"] == "DONE", f"expected DONE, got {run['state']}"
        assert cards == 1, f"expected exactly one slack card, got {cards}"
        assert calls["n"] == 2, "slack notifier should have been retried once"
    finally:
        integrations.NOTIFIERS["slack"] = orig_slack

    # 2. Flaky EMAIL during human approve: first approve records 'failed',
    #    second approve reconciles the claim — exactly one draft, ever.
    ecalls = {"n": 0}
    class FlakyEmail:
        def send(self, *, idempotency_key, payload):
            ecalls["n"] += 1
            if ecalls["n"] == 1:
                raise RuntimeError("simulated gmail blip")
            return DeliveryReceipt(provider_msg_id="ok_" + idempotency_key, channel="email")
    orig_email = integrations.NOTIFIERS["email"]
    integrations.NOTIFIERS["email"] = FlakyEmail()
    try:
        assert human_approve(conn, 1) == "failed", "first approve should fail"
        assert human_approve(conn, 1) == "drafted", "retry should reconcile"
        drafts = conn.execute("SELECT COUNT(*) c FROM outbox WHERE inbound_id=1 "
                              "AND channel='email' AND status='sent'").fetchone()["c"]
        assert drafts == 1, f"expected exactly one draft, got {drafts}"
    finally:
        integrations.NOTIFIERS["email"] = orig_email
    print("  reconcile OK — flaky slack + flaky approve both retried, no dupes")


def test_validation_error_is_retried():
    """A schema-violating reply becomes a retry with a format_error telemetry row."""
    from quinn.schemas import TierVerdict
    state = {"n": 0}

    def flaky_post(body):
        state["n"] += 1
        if state["n"] == 1:
            bad = {"tier": "Scalding", "icp_fit": 2.0, "intent": 0.5}   # invalid enum + range + missing rationale
            content = json.dumps(bad)
        else:
            content = json.dumps({"tier": "Warm", "icp_fit": 0.6, "intent": 0.6,
                                  "signals": [], "disqualifiers": [], "rationale": "ok"})
        return {"choices": [{"message": {"content": content}}],
                "model": body["model"], "usage": {"prompt_tokens": 5, "completion_tokens": 5}}

    llm._post_chat = flaky_post
    recorded = []
    llm.set_recorder(recorded.append)
    try:
        res = llm.complete(task="qualify", system="x", prompt="y", schema=TierVerdict,
                           inbound_id=99, stage="qualify")
        assert res.data.tier == "Warm", "should recover on retry"
        assert state["n"] == 2, "should have retried once"
        outcomes = [r["outcome"] for r in recorded]
        assert "format_error" in outcomes and "ok" in outcomes, outcomes
    finally:
        llm.set_recorder(None)
    print("  validation-retry OK — bad reply -> format_error -> retried -> ok")


def test_human_approval_gate():
    """The two-step human gate: approve creates the draft (exactly once), send
    fires it (exactly once), reject blocks everything downstream forever."""
    llm._post_chat = _fake_post
    from quinn import agent
    from quinn.integrations import human_approve, human_reject, human_send
    conn = _fresh_db()

    # Lead 1 -> DONE means "review card posted" — no draft exists yet, so a
    # send without an approve must refuse outright.
    agent.run_lead(conn, 1, verbose=False)
    no_draft = conn.execute("SELECT COUNT(*) c FROM outbox WHERE inbound_id=1 "
                            "AND channel='email'").fetchone()["c"]
    assert no_draft == 0, "pipeline must not create the draft on its own"
    try:
        human_send(conn, 1)
        raise AssertionError("send before approve should raise")
    except RuntimeError:
        pass

    # Step 1: approve -> draft created, exactly once no matter how many approves.
    assert human_approve(conn, 1) == "drafted"
    assert human_approve(conn, 1) == "already_drafted"   # idempotent re-approve
    # Step 2: send -> exactly once.
    assert human_send(conn, 1) == "sent"
    assert human_send(conn, 1) == "sent"                 # idempotent re-send
    n = conn.execute("SELECT COUNT(*) c FROM outbox "
                     "WHERE idempotency_key='send:1:email'").fetchone()["c"]
    assert n == 1, "exactly one human-send ledger row"
    assert human_reject(conn, 1) == "sent", "can't reject an already-sent mail"

    # Lead 2: reject first -> both approve and send are blocked forever after.
    agent.run_lead(conn, 2, verbose=False)
    assert human_reject(conn, 2) == "rejected"
    assert human_approve(conn, 2) == "rejected", "approve after reject must not draft"
    assert human_send(conn, 2) == "rejected", "send after reject must not fire"
    human_rows = conn.execute("SELECT COUNT(*) c FROM decisions "
                              "WHERE stage='human'").fetchone()["c"]
    assert human_rows == 3, "approve + send + reject all audited"
    # Qualifier topic flowed through to the stored draft.
    d = json.loads(conn.execute(
        "SELECT raw_json FROM decisions WHERE inbound_id=1 AND stage='approve'"
    ).fetchone()["raw_json"])
    assert d["draft"]["topic"] == "messaging"
    print("  human-gate OK — approve once, send once, reject blocks, audited")


ALL = [test_idempotency_and_no_double_spend, test_suppression_blocks_send,
       test_reconcile_after_delivery_failure, test_validation_error_is_retried,
       test_human_approval_gate]


def main():
    for t in ALL:
        print(f"* {t.__name__}")
        t()
    print(f"\nAll {len(ALL)} tests passed.")


if __name__ == "__main__":
    main()
