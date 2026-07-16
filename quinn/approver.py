"""Mail-approval agent — the gate before any send.

Two layers, cheap-first:
  1. Deterministic policy checks (no model): outreach tier, valid recipient, not
     on the suppression/opt-out list, no unresolved merge fields, non-empty body,
     no prior completed send for this lead (belt-and-suspenders vs deliver_once).
  2. LLM review: does the draft make false/hallucinated claims, mismatch the
     tier, or read as spam?

Any failure -> blocked (the run parks in HELD for a human). A pass -> approved.
This is the last line of defense before an irreversible side effect.
"""

from __future__ import annotations

import logging
import re

from quinn import llm, repo
from quinn.obs import event
from quinn.schemas import ApprovalVerdict, EmailDraft, LeadContext

_log = logging.getLogger("quinn.approver")

OUTREACH_TIERS = ("Hot", "Warm", "Mild")
_MERGE_TOKEN = re.compile(r"\{\{.*?\}\}|\[\[.*?\]\]|<[A-Z_]{3,}>")
# Single-bracket placeholders a model sneaks into sign-offs ("[Name]", "[Your
# name]", "[Sender title]") — an email carrying one must never leave.
_PLACEHOLDER = re.compile(r"\[(?:your\s+)?(?:name|sender|title|company)[^\]]*\]",
                          re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SYSTEM = (
    "You are the final approval gate for outbound sales email at Telnyx. Approve "
    "only if the draft is truthful, matches the assigned tier's touch level, is "
    "professional, and is not spammy.\n"
    "TWO grounding checks, both required to approve:\n"
    "  1. Claims about the LEAD must be supported by the lead data.\n"
    "  2. Every claim about TELNYX (products, capabilities, pricing, coverage, "
    "compliance) must be supported by the VERIFIED TELNYX FACTS provided. "
    "Reject if the draft states any Telnyx capability, number, geography, or "
    "certification NOT backed by those facts — that is a hallucination, even if "
    "it sounds plausible.\n"
    "Reject if it invents facts, over-promises, or misrepresents Telnyx."
)


# The gate every draft must pass before a human ever sees it. First the free,
# hard rules (_policy_checks); only if those pass does it spend a model call
# asking "is every claim in this email actually true?". Fail either -> blocked.
def approve(conn, ctx: LeadContext, tier: str, draft: EmailDraft,
            facts_block: str = "") -> tuple[ApprovalVerdict, str]:
    """Return (ApprovalVerdict, model_used).

    `facts_block` is the same verified Telnyx fact set the composer grounded on
    — the approver re-checks the draft's Telnyx claims against it, so a
    hallucinated capability is caught here even if the composer slipped."""
    # ---- Layer 1: deterministic policy checks -------------------------------
    blockers = _policy_checks(conn, ctx, tier, draft)
    # Tool call: suppression-list + policy lookup (a DB tool, not a model).
    event(_log, "tool_call", inbound_id=ctx.inbound_id, tool="policy_checks",
          suppression_and_policy_blockers=len(blockers))
    if blockers:
        return ApprovalVerdict(
            approved=False, issues=blockers,
            reason="failed policy checks: " + "; ".join(blockers),
        ), "policy"

    # ---- Layer 2: LLM review (grounded against the fact base) ---------------
    facts_section = (
        f"== VERIFIED TELNYX FACTS (the draft's Telnyx claims must be backed by "
        f"these) ==\n{facts_block}\n\n" if facts_block else
        "== VERIFIED TELNYX FACTS ==\n(none provided — reject any specific "
        "Telnyx capability claim you cannot otherwise verify.)\n\n"
    )
    prompt = (
        f"{ctx.as_prompt_block()}\n\n"
        f"{facts_section}"
        f"Tier: {tier}\n"
        f"Draft subject: {draft.subject}\n"
        f"Draft body:\n{draft.body}\n\n"
        "Check both grounding rules. Approve or reject this draft. Return JSON."
    )
    res = llm.complete(task="approve", system=SYSTEM, prompt=prompt,
                       schema=ApprovalVerdict, temperature=0.0,
                       inbound_id=ctx.inbound_id, stage="approve")
    return res.data, res.model


# The rule-based checks that cost nothing: right tier, valid email address,
# not on the do-not-contact list, not already emailed, body long enough, no
# leftover placeholders like [Name]. Returns a list of problems (empty = ok).
def _policy_checks(conn, ctx: LeadContext, tier: str, draft: EmailDraft) -> list[str]:
    problems: list[str] = []
    if tier not in OUTREACH_TIERS:
        problems.append(f"tier {tier!r} is not an outreach tier")
    if not _EMAIL_RE.match(ctx.email or ""):
        problems.append(f"invalid recipient email {ctx.email!r}")
    reason = repo.is_suppressed(conn, ctx.email)
    if reason:
        problems.append(f"recipient on suppression list ({reason})")
    if repo.prior_send_exists(conn, ctx.inbound_id, "email"):
        problems.append("a prior email send already exists for this lead")
    body = (draft.body or "").strip()
    if len(body) < 40:
        problems.append("body too short / empty")
    if _MERGE_TOKEN.search(body) or _MERGE_TOKEN.search(draft.subject or ""):
        problems.append("unresolved merge token in draft")
    if _PLACEHOLDER.search(body) or _PLACEHOLDER.search(draft.subject or ""):
        problems.append("placeholder like [Name] left in draft — not sendable")
    if not (draft.subject or "").strip():
        problems.append("missing subject")
    return problems
