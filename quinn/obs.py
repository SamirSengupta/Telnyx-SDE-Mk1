"""Structured logging + persisted event stream (observability §3.2).

What this file does: every pipeline event flows through :func:`event` and goes
to TWO places at once —
  1. stdout, as a single grep-friendly ``event=<name> k=v ...`` log line
     (human-readable for the live demo, machine-parseable for grep/awk); and
  2. the ``pipeline_events`` table, via an injected DB sink (same pattern as
     llm.set_recorder), so the full step-by-step story of every lead — state
     transitions, decisions, retries, deliveries, human actions — is queryable
     after the fact by the CLI (`--trace`) and the web UI's Observability tab.

Stdlib logging only (no structlog — pydantic is the one new dependency this
codebase takes on). The sink is best-effort by contract: an observability
write must never fail a pipeline run.
"""

from __future__ import annotations

import logging
from typing import Callable

_CONFIGURED = False

# Injected persistence sink: fn(name, fields) -> None. Installed per-run by
# agent.run_lead (repo.record_event bound to the run's connection); kept as an
# injection so this module stays free of DB imports.
_event_sink: Callable[[str, dict], None] | None = None


def setup_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    _CONFIGURED = True


def set_event_sink(fn: Callable[[str, dict], None] | None) -> None:
    """Install (or clear) the DB persistence sink for events. Best-effort."""
    global _event_sink
    _event_sink = fn


def event(logger: logging.Logger, name: str, **fields) -> None:
    """Emit `event=<name> k=v ...` to the log AND persist it (if a sink is set)."""
    parts = [f"event={name}"]
    clean = {}
    for k, v in fields.items():
        if v is None:
            continue
        clean[k] = v
        s = str(v).replace("\n", " ")
        if " " in s:
            s = f'"{s}"'
        parts.append(f"{k}={s}")
    logger.info(" ".join(parts))
    if _event_sink is not None:
        try:
            _event_sink(name, clean)
        except Exception as exc:                    # noqa: BLE001 — never fail a run
            logger.warning("event not persisted (%s): %s", name, exc)
