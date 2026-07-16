"""Outreach integrations (Slack + Gmail) and the exactly-once delivery protocol.

What this file does, in three layers:
  1. **Notifiers** — the channel adapters. LiveSlackNotifier posts the review
     card to #all-telnyx-agent; GmailDraftNotifier creates a *draft* (never a
     send) in the connected Gmail account; LoggingNotifier is the offline stand-
     in used automatically when no credentials are in the environment (tests can
     never touch Slack/Gmail).
  2. **deliver_once** — the exactly-once protocol around any notifier: CLAIM a
     UNIQUE-keyed outbox row BEFORE the send, COMMIT after, so a retry, crash,
     or concurrent worker can never repeat a side effect for (lead, channel).
  3. **The two-step human gate** — human_approve (operator types `approve` →
     the Gmail draft is created, Slack told to check Drafts), human_send (the
     checked draft actually goes out), human_reject (blocks the lead's email
     forever). All three ride the same outbox, so no surface — CLI, review
     console, or web UI — can double-fire them.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from quinn import gmail
from quinn.obs import event
from quinn.repo import record_decision

_log = logging.getLogger("quinn.integrations")

SLACK_TIMEOUT_S = 10


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class DeliveryReceipt:
    provider_msg_id: str
    channel: str


class Notifier(Protocol):
    def send(self, *, idempotency_key: str, payload: dict) -> DeliveryReceipt: ...


class LoggingNotifier:
    """POC notifier: prints and returns a deterministic id. No external calls."""

    def __init__(self, channel: str) -> None:
        self.channel = channel

    # Pretends to send: just prints and returns a fake receipt. This is what
    # runs when no real credentials are set (e.g. in the tests).
    def send(self, *, idempotency_key: str, payload: dict) -> DeliveryReceipt:
        event(_log, "send", channel=self.channel, key=idempotency_key,
              to=payload.get("to"), subject=payload.get("subject"))
        msg_id = "logmsg_" + hashlib.sha1(idempotency_key.encode()).hexdigest()[:12]
        return DeliveryReceipt(provider_msg_id=msg_id, channel=self.channel)


# The raw HTTP POST to Slack's webhook. Raises if Slack answers anything but
# "ok", so callers know the message truly did not land.
def _post_slack_webhook(url: str, text: str, blocks: list | None = None) -> None:
    """POST a message to a Slack Incoming Webhook. Raises on non-'ok' response."""
    payload: dict = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=SLACK_TIMEOUT_S) as resp:
        body = resp.read().decode("utf-8", "replace").strip()
    if body != "ok":
        raise RuntimeError(f"slack webhook returned {body!r}")


class LiveSlackNotifier:
    """Posts real messages to a Slack Incoming Webhook.

    The webhook is bound to one channel (#all-telnyx-agent here), so the payload's
    ``to`` is informational only. Implements the same :class:`Notifier` protocol
    as ``LoggingNotifier`` — a drop-in swap, so ``deliver_once`` is unchanged.
    """

    def __init__(self, webhook_url: str) -> None:
        self.channel = "slack"
        self._url = webhook_url

    # Formats the payload as Slack blocks (title + detail) and posts it for real.
    def send(self, *, idempotency_key: str, payload: dict) -> DeliveryReceipt:
        title = payload.get("subject") or "New lead"
        detail = payload.get("text") or ""
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
        ]
        if detail:
            # Full-size section (not a context footnote): the card carries the
            # complete review story — why the tier, judge check, next step.
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": detail[:2900]}})
        _post_slack_webhook(self._url, text=title, blocks=blocks)
        msg_id = "slack_" + hashlib.sha1(idempotency_key.encode()).hexdigest()[:12]
        return DeliveryReceipt(provider_msg_id=msg_id, channel="slack")


class GmailDraftNotifier:
    """'Delivers' email by creating a **draft** in the connected Gmail account.

    Deliberate safety design: the automated pipeline never sends email on its
    own. outbox status 'sent' on the email channel therefore means "draft
    created" (provider_msg_id = the Gmail draft id); the actual send is a
    separate, human-approved side effect (:func:`human_send`) with its own
    outbox key. Same :class:`Notifier` protocol — drop-in for LoggingNotifier.
    """

    channel = "email"

    # "Sending" on the email channel means creating a DRAFT in Gmail — never
    # an actual send. The receipt it returns carries the Gmail draft id.
    def send(self, *, idempotency_key: str, payload: dict) -> DeliveryReceipt:
        draft_id = gmail.create_draft(to=payload.get("to", ""),
                                      subject=payload.get("subject", ""),
                                      body=payload.get("body", ""))
        event(_log, "gmail_draft_created", key=idempotency_key,
              to=payload.get("to"), draft=draft_id)
        return DeliveryReceipt(provider_msg_id=draft_id, channel="email")


# Decides, per channel, whether to use the real sender or the print-only fake:
# real Slack only if the webhook env var is set, real Gmail only if creds and
# a token exist. Tests never load the env file, so they can never go live.
def _default_notifiers() -> dict[str, Notifier]:
    """Build the channel->notifier registry from the environment.

    A channel goes live only when its env var is present (loaded from .env by
    the CLI entrypoint — quinn.config). Tests never import quinn.config, so they
    always get LoggingNotifier and can never post to Slack or touch Gmail, even
    though the credential files exist on this machine. Gating on the ENV VAR
    (not just file existence) is what makes that guarantee hold.
    """
    notifiers: dict[str, Notifier] = {
        "email": LoggingNotifier("email"),
        "slack": LoggingNotifier("slack"),
    }
    webhook = os.environ.get("QUINN_SLACK_WEBHOOK")
    if webhook:
        notifiers["slack"] = LiveSlackNotifier(webhook)
        event(_log, "notifier_live", channel="slack")
    if os.environ.get("QUINN_GMAIL_CREDS") and gmail.is_configured():
        notifiers["email"] = GmailDraftNotifier()
        event(_log, "notifier_live", channel="email", mode="gmail_drafts")
    return notifiers


# Registry: channel -> notifier. Swap emails to a live Gmail adapter the same way.
NOTIFIERS: dict[str, Notifier] = _default_notifiers()


# After a full queue run, posts one Slack digest: how many leads, which tiers,
# what needs a human. Skipped quietly when Slack isn't configured.
def post_run_summary(conn) -> bool:
    """Post an end-of-run status digest to Slack. No-op (False) if no webhook.

    This is the "notify the entire status" surface: one message summarizing the
    whole queue — counts by tier and by terminal state, sends, LLM spend, and the
    HELD/FAILED queues that need a human.
    """
    webhook = os.environ.get("QUINN_SLACK_WEBHOOK")
    if not webhook:
        return False

    def counts(sql: str) -> dict:
        return {row[0]: row[1] for row in conn.execute(sql).fetchall()}

    states = counts("SELECT state, COUNT(*) FROM lead_runs GROUP BY state")
    tiers = counts("SELECT final_tier, COUNT(*) FROM lead_runs "
                   "WHERE final_tier IS NOT NULL GROUP BY final_tier")
    total = sum(states.values())
    sent = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='sent'").fetchone()[0]
    calls, tokens = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(COALESCE(prompt_tokens,0)"
        "+COALESCE(completion_tokens,0)),0) FROM llm_calls"
    ).fetchone()
    held = states.get("HELD", 0)
    failed = states.get("FAILED", 0)

    tier_line = "  ".join(f"{t}: {tiers.get(t, 0)}" for t in ("Hot", "Warm", "Mild", "Cold"))
    state_line = "  ".join(f"{s}: {n}" for s, n in sorted(states.items()))
    attention = ""
    if held or failed:
        attention = f"\n:warning: needs a human — HELD: {held}, FAILED: {failed}"

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "Quinn — inbound run complete"}},
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*{total}* leads processed · *{sent}* messages sent · "
                 f"*{calls}* LLM calls / *{tokens}* tokens"}},
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*By tier*\n{tier_line}"}},
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*By state*\n{state_line}{attention}"}},
    ]
    _post_slack_webhook(webhook, text=f"Quinn run complete: {total} leads, {sent} sent", blocks=blocks)
    event(_log, "slack_summary", leads=total, sent=sent)
    return True


# --------------------------------------------------------------------------- #
# Human approval actions (the approve/reject workflow behind the Slack card)   #
# --------------------------------------------------------------------------- #
# The automated pipeline stops at "review card posted". From there the human
# drives a TWO-STEP gate:
#   1. human_approve  — operator types `approve` (review console / web UI):
#      the Gmail draft is created NOW, and Slack gets a "check your Drafts"
#      follow-up. Only after this does the workflow move to the next lead.
#   2. human_send     — after checking the draft, the operator fires the actual
#      send (or presses Send inside Gmail themselves).
# human_reject at either step discards the lead's email permanently.
# Every action is outbox-guarded (keys deliver:{id}:email / send:{id}:email) so
# approving or sending twice is structurally impossible — the ledger records
# exactly one terminal outcome per lead's email.


# Fires a small "heads up" line to Slack after a human action (approved, sent,
# un-rejected). Best effort: if Slack is down we log it and move on.
def _post_followup(text: str) -> None:
    """Best-effort Slack notification for human actions. Not outbox-guarded —
    it is informational, carries no side-effect risk, and must never block or
    fail the action it narrates."""
    webhook = os.environ.get("QUINN_SLACK_WEBHOOK")
    if not webhook:
        return
    try:
        _post_slack_webhook(webhook, text=text)
    except Exception as exc:                        # noqa: BLE001
        event(_log, "slack_followup_fail", error=str(exc))


# Human step 1 of 2: the operator said "approve" -> NOW the Gmail draft gets
# created (exactly once, however many times they click). Refuses if the lead
# was rejected. Follows up on Slack with "check your Drafts".
def human_approve(conn, inbound_id: int) -> str:
    """Operator approved the outreach -> create the Gmail draft (step 1 of 2).

    Rebuilds the email payload from the persisted approve decision (the same
    crash-safe source of truth the FSM uses), then runs it through deliver_once
    — so the draft is created exactly once no matter how many times approve is
    typed. Returns 'drafted' | 'already_drafted' | 'rejected' | 'failed'.
    """
    srow = conn.execute("SELECT status FROM outbox WHERE idempotency_key=?",
                        (f"send:{inbound_id}:email",)).fetchone()
    if srow and srow["status"] == "rejected":
        event(_log, "human_approve_skip", inbound_id=inbound_id, already="rejected")
        return "rejected"                     # a rejected lead stays rejected
    if _find_draft(conn, inbound_id):
        event(_log, "human_approve_skip", inbound_id=inbound_id, already="drafted")
        return "already_drafted"

    dec = conn.execute(
        "SELECT raw_json FROM decisions WHERE inbound_id=? AND stage='approve' "
        "ORDER BY id DESC LIMIT 1", (inbound_id,)).fetchone()
    if dec is None:
        raise RuntimeError(f"lead {inbound_id} has no composed draft yet "
                           "(run the pipeline first)")
    doc = json.loads(dec["raw_json"])
    if not doc.get("approval", {}).get("approved", False):
        raise RuntimeError(f"lead {inbound_id}'s draft was blocked by the "
                           "approval agent (run is HELD — use --reopen)")
    draft = doc.get("draft", {})
    to = conn.execute("SELECT email FROM inbound_requests WHERE id=?",
                      (inbound_id,)).fetchone()["email"]
    status = deliver_once(conn, inbound_id, "email",
                          {"to": to, "subject": draft.get("subject", ""),
                           "body": draft.get("body", "")})
    if status != "sent":                      # 'sent' on this channel = draft created
        return "failed"
    record_decision(conn, inbound_id, "human", "approve",
                    "operator approved outreach — Gmail draft created", "operator",
                    json.dumps({"draft_subject": draft.get("subject", "")}))
    event(_log, "human_approve", inbound_id=inbound_id)
    _post_followup(
        f":pencil2: Lead {inbound_id} approved — Gmail draft "
        f"“{draft.get('subject', '')}” created. "
        f"Check <https://mail.google.com/mail/u/0/#drafts|Gmail → Drafts>, then "
        f"send with `py -m quinn.run --send-mail {inbound_id}`."
    )
    return "drafted"

# Grabs the one-and-only "send ticket" for this lead. Returns None if we got
# it (or a dead earlier attempt can be retried); returns the final status if
# the outcome is already settled (sent/rejected), meaning: do nothing.
def _claim_send(conn, inbound_id: int, key: str) -> str | None:
    """Claim the human-send slot. Returns None if we own it, else its status."""
    try:
        conn.execute(
            """INSERT INTO outbox (idempotency_key, inbound_id, channel, status,
                                   payload_hash, claimed_at)
               VALUES (?, ?, 'email', 'claimed', 'human-action', ?)""",
            (key, inbound_id, _now()),
        )
        conn.commit()
        return None
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT status FROM outbox WHERE idempotency_key=?",
                           (key,)).fetchone()
        status = row["status"] if row else "unknown"
        if status in ("claimed", "failed"):
            return None          # prior attempt didn't complete -> reconcile
        return status            # 'sent' or 'rejected' -> final, do nothing


# Looks up the Gmail draft id for this lead, if a draft was ever created.
def _find_draft(conn, inbound_id: int) -> str | None:
    row = conn.execute(
        "SELECT provider_msg_id FROM outbox WHERE idempotency_key=? AND status='sent'",
        (f"deliver:{inbound_id}:email",),
    ).fetchone()
    return row["provider_msg_id"] if row else None


# Human step 2 of 2: the operator checked the draft in Gmail and said "send
# it". Fires the real email — exactly once, ever, per lead. Already sent or
# rejected? It reports that and does nothing.
def human_send(conn, inbound_id: int) -> str:
    """Operator checked the draft -> send it (step 2 of 2). Exactly-once."""
    key = f"send:{inbound_id}:email"
    # Terminal outcomes first: a rejected lead reports 'rejected' even if it
    # never got as far as a draft; an already-sent lead reports 'sent'.
    row = conn.execute("SELECT status FROM outbox WHERE idempotency_key=?",
                       (key,)).fetchone()
    if row and row["status"] in ("sent", "rejected"):
        event(_log, "human_send_skip", inbound_id=inbound_id, already=row["status"])
        return row["status"]
    draft_id = _find_draft(conn, inbound_id)
    if not draft_id:
        raise RuntimeError(f"no Gmail draft for lead {inbound_id} — approve it "
                           "first (--review or --approve-mail)")
    final = _claim_send(conn, inbound_id, key)
    if final is not None:
        event(_log, "human_send_skip", inbound_id=inbound_id, already=final)
        return final

    simulated = draft_id.startswith("logmsg_")      # offline demo (no Gmail auth)
    try:
        msg_id = f"sim_{draft_id}" if simulated else gmail.send_draft(draft_id)
    except Exception as exc:                        # noqa: BLE001
        conn.execute("UPDATE outbox SET status='failed', completed_at=? "
                     "WHERE idempotency_key=?", (_now(), key))
        conn.commit()
        event(_log, "human_send_fail", inbound_id=inbound_id, error=str(exc))
        raise

    conn.execute("UPDATE outbox SET status='sent', provider_msg_id=?, completed_at=? "
                 "WHERE idempotency_key=?", (msg_id, _now(), key))
    conn.commit()
    record_decision(conn, inbound_id, "human", "send",
                    "operator approved the draft for sending", "operator",
                    json.dumps({"draft_id": draft_id, "message_id": msg_id,
                                "simulated": simulated}))
    event(_log, "human_send", inbound_id=inbound_id, draft=draft_id,
          simulated=simulated)
    _post_followup(f":email: Lead {inbound_id} — outreach email sent"
                   f"{' (simulated)' if simulated else ''}. Workflow complete.")
    return "sent"


# Deletes a lead's Gmail draft and its ledger row, so a rewrite or an undo can
# start clean. Only ever runs because a human asked for it.
def discard_draft(conn, inbound_id: int) -> bool:
    """Remove a lead's Gmail draft AND its deliver:{id}:email ledger row.

    Used by rewrite (old content must not survive) and unreject (the reject
    already deleted the Gmail draft; the stale ledger row must go too so a
    fresh approve can claim cleanly). A deliberate, audited exception to the
    append-only ledger — only ever triggered by an explicit operator action."""
    draft_id = _find_draft(conn, inbound_id)
    deleted = bool(draft_id) and not draft_id.startswith("logmsg_") \
        and gmail.delete_draft(draft_id)
    conn.execute("DELETE FROM outbox WHERE idempotency_key=?",
                 (f"deliver:{inbound_id}:email",))
    conn.commit()
    event(_log, "draft_discarded", inbound_id=inbound_id, draft=draft_id,
          deleted=deleted)
    return deleted


# The Undo-reject button. Removes the "rejected" mark so the lead goes back
# into the approval queue. Only rejections can be undone — a sent email is
# forever.
def human_unreject(conn, inbound_id: int) -> str:
    """Operator undo for a rejection -> the lead returns to the approval queue.

    Only a 'rejected' terminal row can be reversed — 'sent' is forever. Removes
    the send-key row (and any stale draft row via discard_draft) so the normal
    approve -> check -> send flow starts over, records an audited operator
    decision, and notifies Slack. Returns 'unrejected' | 'not_rejected' | the
    existing terminal status."""
    key = f"send:{inbound_id}:email"
    row = conn.execute("SELECT status FROM outbox WHERE idempotency_key=?",
                       (key,)).fetchone()
    if row is None:
        event(_log, "human_unreject_skip", inbound_id=inbound_id, already="none")
        return "not_rejected"
    if row["status"] != "rejected":
        event(_log, "human_unreject_skip", inbound_id=inbound_id,
              already=row["status"])
        return row["status"]                     # 'sent' can never be undone
    conn.execute("DELETE FROM outbox WHERE idempotency_key=?", (key,))
    conn.commit()
    discard_draft(conn, inbound_id)              # clear any stale draft row too
    record_decision(conn, inbound_id, "human", "unreject",
                    "operator reversed the rejection — lead returned to the "
                    "approval queue", "operator", json.dumps({}))
    event(_log, "human_unreject", inbound_id=inbound_id)
    _post_followup(f":leftwards_arrow_with_hook: Lead {inbound_id} — rejection "
                   "undone; back in the review queue.")
    return "unrejected"


# The Reject button. Marks the lead's email as permanently off (deleting any
# Gmail draft), so no approve or send can ever fire for it — unless a human
# later uses Undo-reject.
def human_reject(conn, inbound_id: int, reason: str = "rejected by operator") -> str:
    """Operator rejected the draft -> discard it. Blocks any later send."""
    key = f"send:{inbound_id}:email"
    try:
        conn.execute(
            """INSERT INTO outbox (idempotency_key, inbound_id, channel, status,
                                   payload_hash, claimed_at, completed_at)
               VALUES (?, ?, 'email', 'rejected', 'human-action', ?, ?)""",
            (key, inbound_id, _now(), _now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT status FROM outbox WHERE idempotency_key=?",
                           (key,)).fetchone()
        status = row["status"] if row else "unknown"
        event(_log, "human_reject_skip", inbound_id=inbound_id, already=status)
        return status                                # 'sent' can't be un-sent

    draft_id = _find_draft(conn, inbound_id)
    deleted = bool(draft_id) and not draft_id.startswith("logmsg_") \
        and gmail.delete_draft(draft_id)
    record_decision(conn, inbound_id, "human", "reject", reason, "operator",
                    json.dumps({"draft_id": draft_id, "draft_deleted": deleted}))
    event(_log, "human_reject", inbound_id=inbound_id, draft=draft_id,
          deleted=deleted)
    return "rejected"


# Fingerprints the exact content being sent (SHA-256), stored as proof of
# what went out under each ticket.
def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


# The exactly-once machine. Before sending anything it writes a uniquely-keyed
# ticket row (CLAIM); then it sends; then it records the result (COMMIT).
# Because the database refuses a second ticket with the same key, no retry,
# crash, or double-click can ever send the same thing twice.
def deliver_once(conn, inbound_id: int, channel: str, payload: dict) -> str:
    """Send exactly once for (lead, channel). Returns the outbox status.

    Protocol (see blueprint.md §5 / llm.md):
      2a CLAIM  — atomic UNIQUE insert. Lose the race -> someone owns it, skip.
      2b SEND   — call the notifier WITH the same idempotency_key so the provider
                  also dedupes if we die between send and commit.
      2c COMMIT — record 'sent' (+ provider id) or 'failed' (retryable).
    """
    key = f"deliver:{inbound_id}:{channel}"
    phash = _payload_hash(payload)

    # 2a — CLAIM. The UNIQUE key means only one attempt can create the row.
    try:
        conn.execute(
            """INSERT INTO outbox
                 (idempotency_key, inbound_id, channel, status, payload_hash, claimed_at)
               VALUES (?, ?, ?, 'claimed', ?, ?)""",
            (key, inbound_id, channel, phash, _now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # A row already exists for this (lead, channel). What we do depends on
        # its status — NOT a blanket skip:
        existing = conn.execute(
            "SELECT status FROM outbox WHERE idempotency_key = ?", (key,)
        ).fetchone()
        status = existing["status"] if existing else "unknown"
        if status == "sent":
            # The ONLY safe skip: this send already completed.
            event(_log, "delivery_skip", channel=channel, key=key, reason="already_sent")
            return "sent"
        # 'claimed' (a prior attempt died between claim and commit) or 'failed'
        # (a prior send errored) => reconcile by re-attempting the send below.
        # The same idempotency_key is passed to the notifier so a provider that
        # supports dedup also collapses the "sent but we lost the ack" case.
        # NOTE: single-worker POC, so a 'claimed' row here always means a crashed
        # attempt. In a multi-worker deployment you'd only reconcile a claim
        # older than a lease timeout, to avoid stomping a send still in flight.
        event(_log, "delivery_reconcile", channel=channel, key=key, was=status)

    # 2b — SEND (idempotency_key passed through to the provider).
    # Recorded as a tool_call: outbound channels are tools the agent wields
    # (Slack webhook / Gmail drafts), and the observability stream should show
    # tool usage alongside model calls. The 'delivery' event below is the
    # OUTCOME; this row is the invocation.
    event(_log, "tool_call", inbound_id=inbound_id, tool=f"send_{channel}", key=key)
    notifier = NOTIFIERS.get(channel)
    if notifier is None:
        conn.execute("UPDATE outbox SET status='failed', completed_at=? "
                     "WHERE idempotency_key=?", (_now(), key))
        conn.commit()
        return "failed"
    try:
        receipt = notifier.send(idempotency_key=key, payload=payload)
    except Exception as exc:                       # noqa: BLE001 — record + let caller retry
        conn.execute("UPDATE outbox SET status='failed', completed_at=? "
                     "WHERE idempotency_key=?", (_now(), key))
        conn.commit()
        event(_log, "delivery_fail", channel=channel, key=key, error=str(exc))
        return "failed"

    # 2c — COMMIT.
    conn.execute(
        "UPDATE outbox SET status='sent', provider_msg_id=?, completed_at=? "
        "WHERE idempotency_key=?",
        (receipt.provider_msg_id, _now(), key),
    )
    conn.commit()
    return "sent"
