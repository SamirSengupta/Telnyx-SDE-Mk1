"""Knowledge retrieval — the grounding layer (RAG without the vector DB).

WHAT THIS FILE DOES: given a lead's topic and free-text ask, it pulls the most
relevant VERIFIED Telnyx facts out of the `knowledge` table and formats them
for injection into the composer and approver prompts. The point: every Telnyx
claim in an outreach email is grounded in a citable fact from the base, not the
model's parametric memory (which can hallucinate a feature or over-claim a
geography — see the East-Africa coverage case).

Retrieval strategy (deliberately simple, stdlib-only — no embeddings/vector DB,
consistent with the project's dependency discipline):
  1. Always include the lead's PRIMARY topic facts + the cross-cutting buckets
     (platform, compliance, coverage) — the one-network story, the certs, and
     the honest footprint are relevant to almost every lead.
  2. Keyword-boost: score every fact by how many of its `tags` appear in the
     lead's request text, so a lead mentioning "HIPAA" or "latency" surfaces
     those facts even from another topic.

In production this is where you'd swap in embeddings + a vector store and a
sync job against Telnyx docs; the retrieve/format contract stays identical, so
nothing upstream changes.
"""

from __future__ import annotations

import re

# Cross-cutting buckets always worth grounding on, regardless of the lead's
# specific topic: the platform story, compliance posture, and the honest
# coverage footprint (so the composer can't over-claim a geography).
_ALWAYS = ("platform", "compliance", "coverage")

_WORD = re.compile(r"[a-z0-9]+")


# Splits text into a set of lowercase words, so two pieces of text can be
# compared by counting the words they share.
def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


# The "which fact-cards fit this lead?" picker. Scores every stored Telnyx
# fact (topic match first, then shared keywords with the lead's request) and
# returns the top handful for the email writer and checker to use.
def retrieve_facts(conn, topic: str, ctx=None, *, limit: int = 8) -> list[dict]:
    """Return the most relevant facts for a lead as a list of dict rows.

    `topic` is the qualifier's primary_topic; `ctx` is the LeadContext (its
    request_for text drives keyword boosting). Facts are ranked: primary-topic
    matches first, then keyword-overlap score, capped at `limit`.
    """
    rows = [dict(r) for r in conn.execute("SELECT * FROM knowledge")]
    if not rows:
        return []

    lead_terms = _tokens(getattr(ctx, "request_for", "") if ctx else "")

    def score(row: dict) -> tuple[int, int, int]:
        topic_match = 2 if row["topic"] == topic else (1 if row["topic"] in _ALWAYS else 0)
        overlap = len(_tokens(row["tags"]) & lead_terms)
        # Sort key: primary topic, then keyword overlap, then always-buckets.
        return (topic_match, overlap, 1 if row["topic"] in _ALWAYS else 0)

    ranked = sorted(rows, key=score, reverse=True)
    # Keep anything that either matches the topic, is a cross-cutting bucket, or
    # has at least one keyword hit — drop pure-noise facts from other topics.
    kept = [r for r in ranked
            if r["topic"] == topic or r["topic"] in _ALWAYS
            or (_tokens(r["tags"]) & lead_terms)]
    return kept[:limit]


# Lays the picked facts out as a numbered list with source links, ready to be
# pasted into a model prompt.
def format_facts(facts: list[dict]) -> str:
    """Render facts as a numbered, cited block for a prompt. Empty -> ''."""
    if not facts:
        return ""
    lines = []
    for i, f in enumerate(facts, 1):
        lines.append(f"[F{i}] ({f['product']}) {f['claim']}  — source: {f['source_url']}")
    return "\n".join(lines)


# Shrinks the facts down to (claim, source) pairs so they can be saved on the
# decision record — that's what the dashboard's Grounding panel shows.
def fact_refs(facts: list[dict]) -> list[dict]:
    """Compact (claim, source) list to persist on a decision for observability."""
    return [{"product": f["product"], "claim": f["claim"], "source_url": f["source_url"]}
            for f in facts]
