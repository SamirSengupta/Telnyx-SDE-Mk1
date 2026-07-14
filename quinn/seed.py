"""Seed the SQLite database with the curated inbound + enrichment dataset.

Idempotent: running it again wipes and re-inserts, so the demo always starts
from a known state.

Usage:
    py -m quinn.seed
"""

from __future__ import annotations

import datetime as _dt

from quinn.db import DB_PATH, get_connection, init_db
from quinn.seed_data import RECORDS

# Suppression / opt-out seeds. "@souqexpress.ae" matches a seeded Hot lead so the
# approver's compliance gate visibly blocks it (parks in HELD); the others are
# generic examples of the kinds of entries this list holds.
SUPPRESSION_SEEDS = (
    ("@souqexpress.ae", "do-not-contact"),
    ("optout@example.com", "opt-out"),
)

# Which authored keys belong to which table. Keeping this explicit (rather than
# "everything not in the other set") makes the split easy to audit.
INBOUND_FIELDS = ("name", "email", "company", "role", "request_for", "source", "created_at")
ENRICHMENT_FIELDS = (
    "seniority",
    "department",
    "dept_headcount",
    "company_employees",
    "industry",
    "hq_region",
    "estimated_revenue_usd",
    "current_provider",
    "monthly_volume",
    "funding_stage",
    "linkedin_url",
)


def seed(conn) -> tuple[int, int]:
    """Insert every record, returning (inbound_count, enrichment_count)."""
    init_db(conn)

    # Fresh start each run so demos are reproducible.
    conn.execute("DELETE FROM enrichment;")
    conn.execute("DELETE FROM inbound_requests;")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('inbound_requests', 'enrichment');")

    inbound_n = enrichment_n = 0
    for rec in RECORDS:
        cur = conn.execute(
            f"""
            INSERT INTO inbound_requests ({", ".join(INBOUND_FIELDS)})
            VALUES ({", ".join("?" for _ in INBOUND_FIELDS)})
            """,
            tuple(rec[f] for f in INBOUND_FIELDS),
        )
        inbound_id = cur.lastrowid
        inbound_n += 1

        # Enrichment mirrors the person/company from the inbound row plus the
        # externally-sourced firmographics.
        conn.execute(
            f"""
            INSERT INTO enrichment
                (inbound_id, person_name, role, company, {", ".join(ENRICHMENT_FIELDS)})
            VALUES (?, ?, ?, ?, {", ".join("?" for _ in ENRICHMENT_FIELDS)})
            """,
            (inbound_id, rec["name"], rec["role"], rec["company"])
            + tuple(rec[f] for f in ENRICHMENT_FIELDS),
        )
        enrichment_n += 1

    # Compliance suppression / opt-out seeds (blueprint §4.5). One matches a real
    # seeded lead's domain so the approver demonstrably parks it in HELD rather
    # than emailing an opted-out recipient — a visible TCPA/GDPR-style gate.
    conn.execute("DELETE FROM suppression_list;")
    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    for pattern, reason in SUPPRESSION_SEEDS:
        conn.execute(
            "INSERT OR IGNORE INTO suppression_list (pattern, reason, created_at) "
            "VALUES (?, ?, ?)",
            (pattern, reason, now),
        )

    conn.commit()
    return inbound_n, enrichment_n


def main() -> None:
    conn = get_connection()
    try:
        inbound_n, enrichment_n = seed(conn)
    finally:
        conn.close()
    print(f"Seeded {inbound_n} inbound requests and {enrichment_n} enrichment rows -> {DB_PATH}")


if __name__ == "__main__":
    main()
