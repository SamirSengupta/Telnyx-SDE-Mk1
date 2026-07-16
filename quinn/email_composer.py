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
    "exclamation spam. Ground the email in the lead's actual details. Plain "
    "text only.\n"
    "GROUNDING RULE (critical): every claim you make about Telnyx's products, "
    "capabilities, pricing, coverage, or compliance MUST be supported by the "
    "VERIFIED TELNYX FACTS provided in the prompt. Do NOT state any Telnyx "
    "capability that is not in those facts — no invented features, numbers, or "
    "geographies. If the facts don't cover something the lead asked for, stay "
    "general or acknowledge you'll confirm it, rather than inventing a claim.\n"
    "Sign every email exactly as:\n"
    "Best,\nQuinn\nTelnyx\n"
    "Never use placeholders like [Name], [Your Name], or {{sender}} anywhere — "
    "the email must be ready to send verbatim."
)


# Looks up which channels (email, slack) a tier is allowed to use.
def channels_for(tier: str) -> list[str]:
    """Which channels this tier gets. Cold never reaches here."""
    return list(TIER_PLAYBOOK.get(tier, (None, []))[1])


# Writes the outreach email. The tier sets the tone (Hot = eager, Mild =
# gentle), the topic picks which products to talk about, and the facts_block
# is the list of verified Telnyx facts the email must stick to. On a rewrite,
# it also gets the old draft plus the operator's feedback to work from.
def compose(ctx: LeadContext, tier: str,
            topic: Topic = "platform_other",
            facts_block: str = "",
            feedback: str = "",
            previous: dict | None = None) -> tuple[EmailDraft, str]:
    """Return (validated EmailDraft, model_used). Raises on non-outreach tiers.

    `facts_block` is the retrieved, verified Telnyx facts (from knowledge.py)
    the email must ground its product claims in — the anti-hallucination layer
    for outbound content. Empty is allowed (degraded: the SYSTEM grounding rule
    still forbids inventing claims, so the email stays generic).

    `feedback` + `previous` power the operator REWRITE action: the previous
    draft and the human's notes are shown to the model so the new draft is a
    directed revision, not a fresh roll of the dice."""
    if tier not in TIER_PLAYBOOK:
        raise ValueError(f"compose called for non-outreach tier {tier!r}")
    touch, _channels = TIER_PLAYBOOK[tier]
    angle = TOPIC_PLAYBOOK.get(topic, TOPIC_PLAYBOOK["platform_other"])
    facts_section = (
        f"\n== VERIFIED TELNYX FACTS (ground every Telnyx claim in these) ==\n"
        f"{facts_block}\n" if facts_block else
        "\n== VERIFIED TELNYX FACTS ==\n(none retrieved — do not state specific "
        "Telnyx capabilities; keep the email general.)\n"
    )
    rewrite_section = ""
    if previous and previous.get("body"):
        rewrite_section += (
            f"\n== PREVIOUS DRAFT (you are rewriting this) ==\n"
            f"Subject: {previous.get('subject', '')}\n{previous.get('body', '')}\n"
        )
    if feedback:
        rewrite_section += (
            f"\n== OPERATOR FEEDBACK (must be incorporated) ==\n{feedback}\n"
        )
    prompt = (
        f"{ctx.as_prompt_block()}\n"
        f"{facts_section}"
        f"{rewrite_section}\n"
        f"Tier: {tier}. Touch guidance: {touch}\n"
        f"Topic: {topic}. Template angle: {angle}\n\n"
        "Write the email. Reference their real situation (what they asked for, "
        "and if present their current provider / volume / industry), and ground "
        "every Telnyx capability claim in the VERIFIED TELNYX FACTS above. "
        "Return JSON."
    )
    # A touch more temperature than decisions — this is writing, not judging.
    res = llm.complete(task="compose", system=SYSTEM, prompt=prompt,
                       schema=EmailDraft, temperature=0.4, max_tokens=700,
                       inbound_id=ctx.inbound_id, stage="compose")
    return res.data, res.model
