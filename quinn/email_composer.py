"""Compose the outreach email for a qualified lead.

Produces content only — it never sends. Two independent routing axes:
  * **Tier** (Hot/Warm/Mild) drives tone, touch level, and channels.
  * **Topic** (why they reached out — classified by the qualifier) drives the
    template: which Telnyx products to lead with and what angle to take. A
    2M-SMS/mo fintech asking about deliverability gets a different email than a
    fleet company asking about IoT SIMs, even at the same tier.

The draft is grounded in real enrichment fields (current_provider,
monthly_volume, industry) so it reads as specific, not generic boilerplate. A
downstream approver (gate) reviews the draft before anything leaves.
"""

from __future__ import annotations

from quinn import llm
from quinn.schemas import EmailDraft, LeadContext, Topic

# tier -> (touch guidance, channels to use). Channels are policy, not model
# output, so they live here and not in the EmailDraft contract.
TIER_PLAYBOOK = {
    "Hot":  ("High-touch and specific. Reference their exact stack and volume, "
             "name the concrete Telnyx products that fit, propose a fast call.",
             ["email", "slack"]),
    "Warm": ("Standard value-led outreach. Concrete next step, one clear CTA.",
             ["email", "slack"]),
    "Mild": ("Light-touch / nurture. Low pressure, share a relevant resource, "
             "soft CTA. Do not push for a call.",
             ["email"]),
}

# topic -> template guidance: the angle + products to lead with. The qualifier
# classifies the inbound ask into one of these (schemas.Topic).
TOPIC_PLAYBOOK: dict[str, str] = {
    "messaging": (
        "Lead with Telnyx Messaging: owned network SMS/MMS, direct carrier "
        "routes for deliverability, 10DLC/A2P compliance support, and volume "
        "pricing. If they're displacing another provider, speak to migration "
        "ease and per-message cost."
    ),
    "voice": (
        "Lead with Programmable Voice: global PoPs for low latency, call "
        "control API, media streaming for AI voice agents, recording and IVR. "
        "If latency or AI agents came up, emphasize the private backbone and "
        "media streaming."
    ),
    "iot_connectivity": (
        "Lead with IoT SIMs: single global SIM with multi-carrier fallback, "
        "one data plan, SIM lifecycle API (activate/suspend), and a dashboard "
        "for fleet management. Speak to coverage and consolidation onto one bill."
    ),
    "verify_2fa": (
        "Lead with Verify API and Number Lookup: OTP over SMS/voice with "
        "high-deliverability routes, fraud screening, and per-verification "
        "pricing. If volumes are high, mention volume tiers."
    ),
    "porting_trunking": (
        "Lead with Elastic SIP Trunking and Porting: self-service bulk number "
        "porting, elastic concurrent-call scaling, global DIDs, and per-minute "
        "rates. Speak to a low-risk migration path off their current carrier."
    ),
    "platform_other": (
        "Lead with the Telnyx platform story: one API and one portal across "
        "voice, messaging, numbers, IoT and Verify — pick the two products most "
        "relevant to their actual ask and stay concrete."
    ),
}

SYSTEM = (
    "You are Quinn, Telnyx's AI SDR. You write outbound sales emails for Telnyx "
    "(programmable voice & SMS, SIP trunking, number porting, IoT SIMs, "
    "Verify/2FA). Write like a sharp human SDR: concise, specific, no fluff, no "
    "exclamation spam. Ground the email in the lead's actual details. Never "
    "invent facts, pricing, or features. Plain text only.\n"
    "Sign every email exactly as:\n"
    "Best,\nQuinn\nTelnyx\n"
    "Never use placeholders like [Name], [Your Name], or {{sender}} anywhere — "
    "the email must be ready to send verbatim."
)


def channels_for(tier: str) -> list[str]:
    """Which channels this tier gets. Cold never reaches here."""
    return list(TIER_PLAYBOOK.get(tier, (None, []))[1])


def compose(ctx: LeadContext, tier: str,
            topic: Topic = "platform_other") -> tuple[EmailDraft, str]:
    """Return (validated EmailDraft, model_used). Raises on non-outreach tiers."""
    if tier not in TIER_PLAYBOOK:
        raise ValueError(f"compose called for non-outreach tier {tier!r}")
    touch, _channels = TIER_PLAYBOOK[tier]
    angle = TOPIC_PLAYBOOK.get(topic, TOPIC_PLAYBOOK["platform_other"])
    prompt = (
        f"{ctx.as_prompt_block()}\n\n"
        f"Tier: {tier}. Touch guidance: {touch}\n"
        f"Topic: {topic}. Template angle: {angle}\n\n"
        "Write the email. Reference their real situation (what they asked for, "
        "and if present their current provider / volume / industry). Return JSON."
    )
    # A touch more temperature than decisions — this is writing, not judging.
    res = llm.complete(task="compose", system=SYSTEM, prompt=prompt,
                       schema=EmailDraft, temperature=0.4, max_tokens=700,
                       inbound_id=ctx.inbound_id, stage="compose")
    return res.data, res.model
