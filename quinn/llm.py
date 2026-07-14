"""Quinn's LLM abstraction layer — the single module that talks to a model.

Everything else in the codebase calls :func:`complete`. Nothing else imports an
HTTP client or knows a provider name. Swapping models, providers, or the proxy
itself is a change confined to this file.

Backend: **CLIProxyAPI** (an OpenAI-compatible multi-LLM proxy running locally).
We hit its ``/v1/chat/completions`` endpoint. Because CLIProxyAPI fronts many
providers behind one OpenAI-shaped API, "use a different model for the judge"
is just a different ``model`` string — see :data:`TASK_MODELS`.

Design points a reviewer should note:
  * **Task-based routing.** Callers pass a logical ``task`` ("qualify", "judge",
    "approve", "compose"); this module maps it to a concrete model.
  * **Pydantic structured output.** When a caller passes a ``BaseModel`` subclass
    as ``schema``, the model is shown the real JSON Schema and its reply is
    parsed AND validated into an instance. A validation failure becomes a typed,
    retryable :class:`LLMFormatError`.
  * **Telemetry choke point.** Every call records one ``llm_calls`` row through
    an injected recorder hook (keeps this module free of DB imports).
  * **Stdlib transport.** ``urllib`` — no third-party HTTP dependency.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

# CLIProxyAPI defaults from the running instance (":8317"). Override via env so
# the same code runs against a different proxy / port without edits.
PROXY_BASE_URL = os.environ.get("QUINN_PROXY_URL", "http://localhost:8317/v1")
# The proxy in this POC is a dummy/local instance; the key is not verified, but
# CLIProxyAPI still expects the OpenAI-style Authorization header to be present.
PROXY_API_KEY = os.environ.get("QUINN_PROXY_KEY", "dummy")
REQUEST_TIMEOUT_S = float(os.environ.get("QUINN_LLM_TIMEOUT", "60"))

# Logical task -> concrete model routed through CLIProxyAPI.
#
# These IDs come from the proxy's live catalog (verified via GET /v1/models).
# Routing is a capability-asymmetry design: Sonnet is the fast, cheap workhorse
# for the first pass (qualify) and the final gate (approve); Opus 4.6 is the
# stronger, more skeptical JUDGE that has the last word on the tier; the composer
# runs on Gemini for stylistic diversity in the writing step. Qualifier and judge
# are the SAME family, DIFFERENT tier — the independence comes from a stronger
# critic + a different prompt + temperature 0, not from a different vendor. Any
# entry is env-overridable, e.g. QUINN_MODEL_JUDGE=gemini-3.1-pro-low.
TASK_MODELS: dict[str, str] = {
    "qualify": os.environ.get("QUINN_MODEL_QUALIFY", "claude-sonnet-4-6"),
    "judge":   os.environ.get("QUINN_MODEL_JUDGE",   "claude-opus-4-6-thinking"),
    "compose": os.environ.get("QUINN_MODEL_COMPOSE", "gemini-3.1-pro-low"),
    "approve": os.environ.get("QUINN_MODEL_APPROVE", "claude-sonnet-4-6"),
}
DEFAULT_MODEL = os.environ.get("QUINN_MODEL_DEFAULT", "claude-sonnet-4-6")

# Bounded retries for transient proxy / format failures.
MAX_ATTEMPTS = int(os.environ.get("QUINN_LLM_RETRIES", "2"))


# --------------------------------------------------------------------------- #
# Types                                                                       #
# --------------------------------------------------------------------------- #

class LLMError(RuntimeError):
    """Base for anything that goes wrong talking to a model."""


class LLMTransportError(LLMError):
    """Network / proxy failure (timeout, connection refused, 5xx). Retryable."""


class LLMFormatError(LLMError):
    """Model output failed to parse or validate against the schema. Retryable."""


@dataclass
class LLMResult:
    text: str                          # raw assistant text
    data: Any = None                   # validated pydantic instance (or None)
    model: str = ""                    # concrete model the proxy used
    usage: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Telemetry hook (observability §3.1)                                          #
# --------------------------------------------------------------------------- #
# llm.py is the single choke point for model calls, so it is the single place we
# record per-call telemetry. To keep this module free of DB imports, the DB
# write is injected as a recorder callable at startup (see repo.record_llm_call
# / agent.run_lead). A telemetry failure must never break a pipeline run.
_recorder: Callable[[dict], None] | None = None


def set_recorder(fn: Callable[[dict], None] | None) -> None:
    """Install (or clear) the per-call telemetry sink. Best-effort."""
    global _recorder
    _recorder = fn


def _record(row: dict) -> None:
    if _recorder is None:
        return
    try:
        _recorder(row)
    except Exception as exc:                        # noqa: BLE001 — never fail a run
        print(f"    [telemetry-warn] llm_call not recorded: {exc}")


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #

# Operator overrides from the dashboard's "Configure Agent" tab. Stored as a
# tiny JSON file ({task: model}) so a change made in the web UI applies to
# every process — the CLI, the review console, and the web server all read it
# at call time. File absent or unreadable -> code defaults above apply.
MODEL_CONFIG_PATH = Path(os.environ.get(
    "QUINN_MODEL_CONFIG",
    Path(__file__).resolve().parent.parent / "data" / "models.json"))


def model_overrides() -> dict[str, str]:
    """Current operator overrides ({task: model}); {} when none are set."""
    try:
        data = json.loads(MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def set_model_override(task: str, model: str | None) -> None:
    """Persist (or clear, with model=None) one task's model override."""
    if task not in TASK_MODELS:
        raise ValueError(f"unknown task {task!r}")
    overrides = model_overrides()
    if model:
        overrides[task] = model
    else:
        overrides.pop(task, None)
    MODEL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_CONFIG_PATH.write_text(json.dumps(overrides, indent=2), encoding="utf-8")


def model_for(task: str) -> str:
    """Return the concrete model id a logical task routes to.

    Precedence: dashboard override (models.json) > env var > code default."""
    override = model_overrides().get(task)
    if override:
        return override
    return TASK_MODELS.get(task, DEFAULT_MODEL)


def complete(
    *,
    task: str,
    system: str,
    prompt: str,
    schema: type[BaseModel] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1500,
    inbound_id: int | None = None,
    stage: str | None = None,
) -> LLMResult:
    """Single entrypoint for all model calls.

    Args:
        task: logical task name; selects the model via :data:`TASK_MODELS`.
        system: system prompt (role/instructions).
        prompt: user prompt (the actual lead context / question).
        schema: optional pydantic ``BaseModel`` subclass. When given, the model
            is shown the model's real JSON Schema and its reply is parsed AND
            validated into an instance on ``LLMResult.data``. A validation
            failure is a (retryable) :class:`LLMFormatError`.
        temperature: sampling temperature (low by default — we want stable,
            defensible decisions, not creativity, except in the composer).
        inbound_id, stage: correlation for the ``llm_calls`` telemetry row.

    Raises:
        LLMTransportError: proxy unreachable / errored (retryable).
        LLMFormatError: schema requested but reply didn't parse/validate (retryable).
    """
    model = model_for(task)
    sys_prompt = system
    if schema is not None:
        sys_prompt = (
            system
            + "\n\nYou MUST respond with a single JSON object and nothing else — "
              "no markdown, no code fences, no prose. It must conform to this "
              "JSON Schema:\n"
            + json.dumps(schema.model_json_schema(), indent=2)
        )

    body = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ],
    }

    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # The system prompt actually sent THIS attempt (a format-error retry
        # appends the rejection nudge) — telemetry records the real payload.
        sent_system = body["messages"][0]["content"]
        t0 = time.monotonic()
        try:
            raw = _post_chat(body)
        except LLMTransportError as exc:
            last_exc = exc
            _record(_telemetry_row(inbound_id, stage, task, model, "",
                                   {}, t0, attempt, "transport_error",
                                   sent_system, prompt, ""))
            if attempt < MAX_ATTEMPTS:           # no point sleeping before giving up
                time.sleep(min(2 ** attempt, 8))  # bounded backoff
            continue

        text = _extract_text(raw)
        used_model = raw.get("model", model)
        usage = raw.get("usage", {})

        if schema is None:
            _record(_telemetry_row(inbound_id, stage, task, model, used_model,
                                   usage, t0, attempt, "ok",
                                   sent_system, prompt, text))
            return LLMResult(text=text, data=None, model=used_model, usage=usage)

        try:
            parsed = _parse_json(text)
            data = schema.model_validate(parsed)     # pydantic: typed + coerced
        except (LLMFormatError, ValidationError) as exc:
            last_exc = LLMFormatError(str(exc)) if isinstance(exc, ValidationError) else exc
            _record(_telemetry_row(inbound_id, stage, task, model, used_model,
                                   usage, t0, attempt, "format_error",
                                   sent_system, prompt, text))
            # Nudge the model harder on the retry, telling it WHAT was wrong.
            body["messages"][0]["content"] = (
                sys_prompt + "\n\nYour previous reply was rejected: "
                f"{str(exc)[:300]}. Return ONLY a valid JSON object that "
                "conforms to the schema."
            )
            continue

        _record(_telemetry_row(inbound_id, stage, task, model, used_model,
                               usage, t0, attempt, "ok",
                               sent_system, prompt, text))
        return LLMResult(text=text, data=data, model=used_model, usage=usage)

    assert last_exc is not None
    raise last_exc


def _telemetry_row(inbound_id, stage, task, requested_model, used_model,
                   usage, t0, attempt, outcome,
                   system_prompt="", user_prompt="", response_text="") -> dict:
    return {
        "inbound_id": inbound_id,
        "stage": stage,
        "task": task,
        "requested_model": requested_model,
        "used_model": used_model,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "attempt": attempt,
        "outcome": outcome,
        # Full payload capture — what was actually said to/by the model. This is
        # what makes a decision auditable after the fact, not just countable.
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "response_text": response_text,
    }


# --------------------------------------------------------------------------- #
# Transport (kept private — the rest of the app never sees HTTP)              #
# --------------------------------------------------------------------------- #

def _post_chat(body: dict) -> dict:
    url = f"{PROXY_BASE_URL}/chat/completions"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {PROXY_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise LLMTransportError(f"proxy HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise LLMTransportError(f"proxy unreachable at {url}: {exc}") from exc


def _extract_text(raw: dict) -> str:
    try:
        return raw["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMFormatError(f"unexpected proxy response shape: {raw!r}") from exc


def _parse_json(text: str) -> dict:
    """Parse a JSON object out of model text, tolerating stray fences/prose."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("{"):]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMFormatError(f"no JSON object found in reply: {text[:200]!r}")
    try:
        obj = json.loads(s[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMFormatError(f"invalid JSON: {exc}; got {text[:200]!r}") from exc
    if not isinstance(obj, dict):
        raise LLMFormatError("parsed JSON was not an object")
    return obj
