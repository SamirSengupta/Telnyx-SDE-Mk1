"""Typed contracts for every probabilistic <-> deterministic boundary.

Every place a model hands data back to the pipeline is a pydantic ``BaseModel``
here. One validation idiom, in one file, so the contracts are visible at a
glance. ``llm.complete`` generates the prompt's JSON Schema from these models
(``model_json_schema()``) and validates replies against them — a schema
violation becomes a typed, *retryable* :class:`quinn.llm.LLMFormatError` instead
of a silently-defaulted dict.

This is the POC's first third-party dependency. Stdlib-first is the rule; the
LLM boundary is exactly where validation earns its keep.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Tier = Literal["Hot", "Warm", "Mild", "Cold"]

# Why the lead reached out — routes the composer to a topic-specific template.
# Mirrors Telnyx's product lines; "platform_other" is the catch-all.
Topic = Literal[
    "messaging",          # SMS/MMS, A2P, campaigns, deliverability
    "voice",              # programmable voice, IVR, call control, AI voice agents
    "iot_connectivity",   # IoT SIMs, M2M, fleet/device connectivity
    "verify_2fa",         # Verify API, OTP, number lookup, fraud
    "porting_trunking",   # SIP trunking, number porting, DIDs, PBX
    "platform_other",     # multi-product, fax, unclear, or anything else
]


# --------------------------------------------------------------------------- #
# Model-output contracts (what an LLM must return at each stage)              #
# --------------------------------------------------------------------------- #

class TierVerdict(BaseModel):
    """Qualifier output — the first-pass tier assignment + topic routing."""
    model_config = ConfigDict(extra="ignore")

    tier: Tier
    # Primary reason for the inbound ask. Defaulted so verdicts recorded before
    # this field existed (and minimal fakes) still validate.
    primary_topic: Topic = "platform_other"
    icp_fit: float = Field(ge=0.0, le=1.0)
    intent: float = Field(ge=0.0, le=1.0)
    signals: list[str] = Field(default_factory=list)
    disqualifiers: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)


class JudgeVerdict(BaseModel):
    """Judge output — the authoritative tier + hallucination review."""
    model_config = ConfigDict(extra="ignore")

    agree: bool
    final_tier: Tier
    changed: bool = False
    hallucination_flags: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class EmailDraft(BaseModel):
    """Composer output. Channels are playbook policy, NOT model output, so they
    live outside this contract (see email_composer.channels_for)."""
    model_config = ConfigDict(extra="ignore")

    subject: str = Field(min_length=1)
    body: str = Field(min_length=40)
    grounded_facts: list[str] = Field(default_factory=list)


class ApprovalVerdict(BaseModel):
    """Approver LLM-review output (layer 2 of the gate)."""
    model_config = ConfigDict(extra="ignore")

    approved: bool
    issues: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


# --------------------------------------------------------------------------- #
# LeadContext — the immutable view an agent reasons over                      #
# --------------------------------------------------------------------------- #

class LeadContext(BaseModel):
    """Frozen, validated join of inbound_requests x enrichment.

    A pydantic model (not a dataclass) so DB reads are validated for free and
    ``.model_dump()`` is available for logging. ``as_prompt_block`` stays a
    method — it's how the lead is rendered into a prompt.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    inbound_id: int
    # inbound (self-reported)
    name: str
    email: str
    company: str
    role: str
    request_for: str
    source: str
    created_at: str
    # enrichment (external) — may be absent -> degraded mode
    enrichment_present: bool
    seniority: str | None = None
    department: str | None = None
    dept_headcount: int | None = None
    company_employees: int | None = None
    industry: str | None = None
    hq_region: str | None = None
    estimated_revenue_usd: str | None = None
    current_provider: str | None = None
    monthly_volume: str | None = None
    funding_stage: str | None = None
    linkedin_url: str | None = None

    def as_prompt_block(self) -> str:
        """Render the lead as a compact, unambiguous block for a model prompt."""
        lines = [
            "== INBOUND (self-reported) ==",
            f"name: {self.name}",
            f"email: {self.email}",
            f"company: {self.company}",
            f"role: {self.role}",
            f"source: {self.source}",
            f'request_for: "{self.request_for}"',
        ]
        if self.enrichment_present:
            lines += [
                "",
                "== ENRICHMENT (external firmographics) ==",
                f"seniority: {self.seniority}",
                f"department: {self.department}",
                f"dept_headcount: {self.dept_headcount}",
                f"company_employees: {self.company_employees}",
                f"industry: {self.industry}",
                f"hq_region: {self.hq_region}",
                f"estimated_revenue_usd: {self.estimated_revenue_usd}",
                f"current_provider: {self.current_provider}",
                f"monthly_volume: {self.monthly_volume}",
                f"funding_stage: {self.funding_stage}",
            ]
        else:
            lines += ["", "== ENRICHMENT ==", "(none found — treat as a weaker signal)"]
        return "\n".join(lines)
