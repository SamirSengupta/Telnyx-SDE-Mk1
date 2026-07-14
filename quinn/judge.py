"""LLM-as-judge — the anti-hallucination second opinion.

The qualifier (agent.py) assigns a tier. The judge independently re-reads the
same LeadContext PLUS the qualifier's verdict and decides whether to confirm,
amend, or veto it. It runs on a stronger, more skeptical model than the
qualifier (see llm.TASK_MODELS) at temperature 0 — a genuine critic, not a
rubber stamp.

Posture: conservative gate. The judge may freely downgrade; upgrades require a
strong, explicit reason. Any disqualifier the qualifier missed (competitor,
spam/unsolicited bulk, student/side-project, VC diligence, obvious non-buyer)
is grounds to veto to Cold.
"""

from __future__ import annotations

from quinn import llm
from quinn.schemas import JudgeVerdict, LeadContext, Tier

TIERS = ("Hot", "Warm", "Mild", "Cold")

SYSTEM = (
    "You are Quinn's QA judge for a B2B telecom/CPaaS (Telnyx) sales pipeline. "
    "A first model assigned a lead tier. Your job is to catch mistakes and "
    "hallucinations, not to be agreeable. Verify every claim in the qualifier's "
    "rationale against the provided lead data; if a claim is not supported by the "
    "data, flag it. Be conservative: downgrade freely, and veto to Cold for any "
    "disqualifier (competitor/CPaaS vendor doing recon, unsolicited bulk SMS, "
    "student or hobby side-project, VC diligence, clear non-buyer). Only uphold a "
    "high tier when ICP fit AND buying intent are both genuinely evidenced."
)


def judge(ctx: LeadContext, qualifier_verdict: dict) -> tuple[JudgeVerdict, str]:
    """Return (validated JudgeVerdict, model_used). `final_tier` is authoritative."""
    prompt = (
        f"{ctx.as_prompt_block()}\n\n"
        f"== QUALIFIER VERDICT (under review) ==\n"
        f"tier: {qualifier_verdict.get('tier')}\n"
        f"icp_fit: {qualifier_verdict.get('icp_fit')}\n"
        f"intent: {qualifier_verdict.get('intent')}\n"
        f"signals: {qualifier_verdict.get('signals')}\n"
        f"disqualifiers: {qualifier_verdict.get('disqualifiers')}\n"
        f"rationale: {qualifier_verdict.get('rationale')}\n\n"
        "Confirm, amend, or veto. Return the JSON verdict."
    )
    try:
        res = llm.complete(task="judge", system=SYSTEM, prompt=prompt,
                           schema=JudgeVerdict, temperature=0.0,
                           inbound_id=ctx.inbound_id, stage="judge")
        verdict: JudgeVerdict = res.data
        model = res.model
    except llm.LLMError:
        # If the judge is unavailable/unparseable after retries, fall back
        # conservatively — NEVER invent a hotter tier than the qualifier gave.
        qtier = qualifier_verdict.get("tier")
        verdict = JudgeVerdict(
            agree=False,
            final_tier=qtier if qtier in TIERS else "Cold",
            changed=False,
            hallucination_flags=["judge unavailable — deferred to qualifier tier"],
            reason="judge call failed after retries; conservative fallback",
        )
        model = "fallback"

    # Business rule: keep `changed` honest relative to the qualifier.
    q = qualifier_verdict.get("tier")
    if verdict.changed != (verdict.final_tier != q):
        verdict = verdict.model_copy(update={"changed": verdict.final_tier != q})
    return verdict, model
