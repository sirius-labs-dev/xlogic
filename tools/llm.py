"""LLM decision layer — OpenAI chat, fail-closed to HOLD on any error."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)
from pydantic import BaseModel, Field

import config

# Structured output via SDK chat.completions.parse() (openai>=1.x / 3.x).
# This helper sends response_format as a strict json_schema derived from the
# Pydantic model (see OpenAI Python helpers.md — parse() wrapper over create()).
# Verified locally: OpenAI(...).chat.completions.parse exists in openai==3.13.0.

load_dotenv()

SYSTEM_PROMPT = (
    "Temkinli bir spot trader'sin. Emin degilsen HOLD de. Nakitte beklemek iyi bir karardir. "
    "Sadece verilen sinyallere dayan, disaridan bilgi uydurma. Miktar BELIRLEME; onu risk "
    "sistemi hesaplar. Sadece long; acik satis yok."
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

MAX_ATTEMPTS = 3  # 1 initial + 2 retries
RETRY_WAITS = (1.0, 3.0)
CALL_TIMEOUT_SEC = 20.0
BUDGET_SEC = 45.0


class DecisionSchema(BaseModel):
    action: Literal["BUY", "SELL", "HOLD"]
    instId: str
    entry_px: float
    stop_px: float
    take_profit_px: float
    confidence: float = Field(ge=0, le=1)
    reason: str


def _hold(
    code: str,
    *,
    latency_ms: int = 0,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "action": "HOLD",
        "instId": "",
        "entry_px": 0.0,
        "stop_px": 0.0,
        "take_profit_px": 0.0,
        "confidence": 0.0,
        "reason": str(code)[:200],
        "llm_ok": False,
        "latency_ms": int(latency_ms),
        "usage": usage or {},
    }


def _provider() -> str:
    """Prefer official Anthropic when ANTHROPIC_API_KEY is set."""
    import os

    if (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        return "anthropic"
    if (os.getenv("OPENAI_API_KEY") or "").strip():
        return "openai"
    return "none"


def _get_api_key() -> str:
    import os

    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise ValueError("OPENAI_API_KEY bulunamadi")
    return key


def _get_anthropic_key() -> str:
    import os

    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise ValueError("ANTHROPIC_API_KEY bulunamadi")
    return key


def _get_base_url() -> str | None:
    """OpenAI-compatible base URL from env only (no hardcoded gateway)."""
    import os

    url = (os.getenv("OPENAI_BASE_URL") or "").strip()
    return url.rstrip("/") or None


def _get_anthropic_base_url() -> str | None:
    """Only honor http(s) base URLs; ignore mistaken non-URL values."""
    import os

    url = (os.getenv("ANTHROPIC_BASE_URL") or "").strip().rstrip("/")
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return None


def _client() -> OpenAI:
    # Never log / print the key.
    kwargs: dict[str, Any] = {
        "api_key": _get_api_key(),
        "timeout": CALL_TIMEOUT_SEC,
    }
    base = _get_base_url()
    if base:
        kwargs["base_url"] = base
    return OpenAI(**kwargs)


def _anthropic_client() -> Any:
    from anthropic import Anthropic

    kwargs: dict[str, Any] = {
        "api_key": _get_anthropic_key(),
        "timeout": CALL_TIMEOUT_SEC,
    }
    base = _get_anthropic_base_url()
    if base:
        kwargs["base_url"] = base
    return Anthropic(**kwargs)


def _usage_dict(completion: Any) -> dict[str, Any]:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return {}
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _strip_fences(text: str) -> str:
    text = text.strip()
    if "```" in text:
        text = _FENCE_RE.sub("", text).strip()
        # Also handle ```json ... ``` blocks in the middle
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
        if m:
            text = m.group(1).strip()
    return text


def _validate_decision(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return cleaned decision dict, or None if invalid (caller -> HOLD)."""
    data = dict(data)
    data.pop("size", None)
    data.pop("sz", None)
    data.pop("quantity", None)
    data.pop("qty", None)

    action = data.get("action")
    if action not in ("BUY", "SELL", "HOLD"):
        return None

    inst_id = str(data.get("instId") or "")
    if inst_id and inst_id not in config.ALLOWED_PAIRS:
        return None
    if action in ("BUY", "SELL") and inst_id not in config.ALLOWED_PAIRS:
        return None

    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0 or confidence > 1:
        confidence = 0.0

    reason = str(data.get("reason") or "")[:200]

    entry_px = data.get("entry_px", 0)
    stop_px = data.get("stop_px", 0)
    take_profit_px = data.get("take_profit_px", 0)

    if action == "BUY":
        try:
            entry_px = float(entry_px)
            stop_px = float(stop_px)
            take_profit_px = float(take_profit_px)
        except (TypeError, ValueError):
            return None
        if not (entry_px > 0 and stop_px > 0 and take_profit_px > 0):
            return None
        if not (stop_px < entry_px < take_profit_px):
            return None
    else:
        try:
            entry_px = float(entry_px or 0)
            stop_px = float(stop_px or 0)
            take_profit_px = float(take_profit_px or 0)
        except (TypeError, ValueError):
            entry_px = stop_px = take_profit_px = 0.0

    return {
        "action": action,
        "instId": inst_id,
        "entry_px": entry_px,
        "stop_px": stop_px,
        "take_profit_px": take_profit_px,
        "confidence": confidence,
        "reason": reason,
    }


def _parse_content_fallback(content: str) -> dict[str, Any] | None:
    raw = _strip_fences(content)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _user_payload(signals: dict, account: dict) -> dict[str, Any]:
    return {
        "signals": signals,
        "account": account,
        "allowed_pairs": list(config.ALLOWED_PAIRS),
        "instructions": (
            "Return only the decision JSON. Do not choose size. "
            "If unsure, action=HOLD."
        ),
    }


def _ask_anthropic(signals: dict, account: dict) -> dict[str, Any]:
    """Official Anthropic Messages API — fail-closed to HOLD."""
    from anthropic import (
        APIConnectionError as AnthConnectionError,
        APIStatusError as AnthStatusError,
        APITimeoutError as AnthTimeoutError,
        AuthenticationError as AnthAuthError,
        RateLimitError as AnthRateLimitError,
    )

    started = time.monotonic()
    usage_acc: dict[str, Any] = {}
    try:
        client = _anthropic_client()
    except ValueError:
        return _hold("LLM_AUTH", latency_ms=0)
    except Exception:
        return _hold("LLM_UNKNOWN", latency_ms=0)

    user_text = (
        json.dumps(_user_payload(signals, account), ensure_ascii=False, indent=2)
        + "\n\nRespond with a single JSON object matching keys: "
        "action, instId, entry_px, stop_px, take_profit_px, confidence, reason."
    )
    last_error_code = "LLM_UNKNOWN"
    attempt = 0
    while attempt < MAX_ATTEMPTS:
        elapsed = time.monotonic() - started
        if elapsed >= BUDGET_SEC:
            break
        try:
            msg = client.messages.create(
                model=config.LLM_MODEL,
                max_tokens=300,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_text}],
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            usage = getattr(msg, "usage", None)
            if usage is not None:
                usage_acc = {
                    "prompt_tokens": getattr(usage, "input_tokens", None),
                    "completion_tokens": getattr(usage, "output_tokens", None),
                    "total_tokens": (
                        (getattr(usage, "input_tokens", 0) or 0)
                        + (getattr(usage, "output_tokens", 0) or 0)
                    ),
                }
            parts: list[str] = []
            for block in getattr(msg, "content", None) or []:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
            content = "\n".join(parts).strip()
            raw = _parse_content_fallback(content)
            if raw is None:
                return _hold("LLM_BAD_JSON", latency_ms=latency_ms, usage=usage_acc)
            cleaned = _validate_decision(raw)
            if cleaned is None:
                return _hold("LLM_BAD_SCHEMA", latency_ms=latency_ms, usage=usage_acc)
            return {
                **cleaned,
                "llm_ok": True,
                "latency_ms": latency_ms,
                "usage": usage_acc,
                "provider": "anthropic",
            }
        except AnthAuthError:
            latency_ms = int((time.monotonic() - started) * 1000)
            return _hold("LLM_AUTH", latency_ms=latency_ms, usage=usage_acc)
        except AnthTimeoutError:
            latency_ms = int((time.monotonic() - started) * 1000)
            return _hold("LLM_TIMEOUT", latency_ms=latency_ms, usage=usage_acc)
        except AnthRateLimitError:
            last_error_code = "LLM_RATE_LIMIT"
        except AnthStatusError as exc:
            status = int(getattr(exc, "status_code", 0) or 0)
            if status in (401, 403):
                latency_ms = int((time.monotonic() - started) * 1000)
                return _hold("LLM_AUTH", latency_ms=latency_ms, usage=usage_acc)
            if status == 429:
                last_error_code = "LLM_RATE_LIMIT"
            elif 500 <= status < 600:
                last_error_code = "LLM_SERVER"
            else:
                latency_ms = int((time.monotonic() - started) * 1000)
                return _hold("LLM_UNKNOWN", latency_ms=latency_ms, usage=usage_acc)
        except AnthConnectionError:
            last_error_code = "LLM_SERVER"
        except Exception:
            latency_ms = int((time.monotonic() - started) * 1000)
            return _hold("LLM_UNKNOWN", latency_ms=latency_ms, usage=usage_acc)

        if attempt >= MAX_ATTEMPTS - 1:
            break
        wait = RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]
        if time.monotonic() - started + wait >= BUDGET_SEC:
            break
        time.sleep(wait)
        attempt += 1

    latency_ms = int((time.monotonic() - started) * 1000)
    return _hold(last_error_code, latency_ms=latency_ms, usage=usage_acc)


def ask_decision(signals: dict, account: dict) -> dict[str, Any]:
    """
    Ask the LLM for BUY/SELL/HOLD. Any failure -> HOLD with llm_ok=False.
    Prefers ANTHROPIC_API_KEY (official API); else OpenAI-compatible gateway.
    """
    if _provider() == "anthropic":
        return _ask_anthropic(signals, account)
    if _provider() == "none":
        return _hold("LLM_AUTH", latency_ms=0)

    started = time.monotonic()
    usage_acc: dict[str, Any] = {}

    try:
        client = _client()
    except ValueError:
        return _hold("LLM_AUTH", latency_ms=0)
    except Exception:
        return _hold("LLM_UNKNOWN", latency_ms=0)

    user_payload = _user_payload(signals, account)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, indent=2),
        },
    ]

    last_error_code = "LLM_UNKNOWN"
    attempt = 0

    while attempt < MAX_ATTEMPTS:
        elapsed = time.monotonic() - started
        if elapsed >= BUDGET_SEC:
            break

        try:
            # Prefer strict structured output via parse() (json_schema under the hood).
            # Some OpenAI-compatible gateways may not support it; fall back to
            # response_format json_object + manual parse.
            try:
                completion = client.chat.completions.parse(
                    model=config.LLM_MODEL,
                    messages=messages,
                    response_format=DecisionSchema,
                    temperature=0,
                    max_tokens=300,
                    timeout=CALL_TIMEOUT_SEC,
                )
                usage_acc = _usage_dict(completion)
                latency_ms = int((time.monotonic() - started) * 1000)
                message = completion.choices[0].message
                parsed = getattr(message, "parsed", None)
                if parsed is not None:
                    data = parsed.model_dump()
                else:
                    content = getattr(message, "content", None) or ""
                    if not content:
                        return _hold(
                            "LLM_BAD_SCHEMA", latency_ms=latency_ms, usage=usage_acc
                        )
                    raw = _parse_content_fallback(content)
                    if raw is None:
                        return _hold(
                            "LLM_BAD_JSON", latency_ms=latency_ms, usage=usage_acc
                        )
                    data = raw
            except Exception as parse_exc:
                # If auth/timeout/rate — re-raise to outer handlers
                if isinstance(
                    parse_exc,
                    (
                        AuthenticationError,
                        APITimeoutError,
                        RateLimitError,
                        APIStatusError,
                        APIConnectionError,
                    ),
                ):
                    raise
                # Gateway may reject json_schema; use json_object mode instead.
                # (Documented OpenAI fallback: response_format={"type":"json_object"})
                completion = client.chat.completions.create(
                    model=config.LLM_MODEL,
                    messages=messages
                    + [
                        {
                            "role": "user",
                            "content": (
                                "Respond with a single JSON object matching keys: "
                                "action, instId, entry_px, stop_px, take_profit_px, "
                                "confidence, reason."
                            ),
                        }
                    ],
                    response_format={"type": "json_object"},
                    temperature=0,
                    max_tokens=300,
                    timeout=CALL_TIMEOUT_SEC,
                )
                usage_acc = _usage_dict(completion)
                latency_ms = int((time.monotonic() - started) * 1000)
                content = completion.choices[0].message.content or ""
                raw = _parse_content_fallback(content)
                if raw is None:
                    return _hold(
                        "LLM_BAD_JSON", latency_ms=latency_ms, usage=usage_acc
                    )
                data = raw

            cleaned = _validate_decision(data)
            if cleaned is None:
                return _hold("LLM_BAD_SCHEMA", latency_ms=latency_ms, usage=usage_acc)

            return {
                **cleaned,
                "llm_ok": True,
                "latency_ms": latency_ms,
                "usage": usage_acc,
            }

        except AuthenticationError:
            latency_ms = int((time.monotonic() - started) * 1000)
            return _hold("LLM_AUTH", latency_ms=latency_ms, usage=usage_acc)

        except APITimeoutError:
            latency_ms = int((time.monotonic() - started) * 1000)
            return _hold("LLM_TIMEOUT", latency_ms=latency_ms, usage=usage_acc)

        except RateLimitError:
            last_error_code = "LLM_RATE_LIMIT"
            if attempt >= MAX_ATTEMPTS - 1:
                break
            wait = RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]
            if time.monotonic() - started + wait >= BUDGET_SEC:
                break
            time.sleep(wait)
            attempt += 1
            continue

        except APIStatusError as exc:
            status = getattr(exc, "status_code", None) or 0
            if status == 401 or status == 403:
                latency_ms = int((time.monotonic() - started) * 1000)
                return _hold("LLM_AUTH", latency_ms=latency_ms, usage=usage_acc)
            if status == 429:
                last_error_code = "LLM_RATE_LIMIT"
            elif 500 <= int(status) < 600:
                last_error_code = "LLM_SERVER"
            else:
                latency_ms = int((time.monotonic() - started) * 1000)
                return _hold("LLM_UNKNOWN", latency_ms=latency_ms, usage=usage_acc)

            if attempt >= MAX_ATTEMPTS - 1:
                break
            wait = RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]
            if time.monotonic() - started + wait >= BUDGET_SEC:
                break
            time.sleep(wait)
            attempt += 1
            continue

        except APIConnectionError:
            last_error_code = "LLM_SERVER"
            if attempt >= MAX_ATTEMPTS - 1:
                break
            wait = RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]
            if time.monotonic() - started + wait >= BUDGET_SEC:
                break
            time.sleep(wait)
            attempt += 1
            continue

        except (json.JSONDecodeError, ValueError):
            latency_ms = int((time.monotonic() - started) * 1000)
            return _hold("LLM_BAD_JSON", latency_ms=latency_ms, usage=usage_acc)

        except Exception:
            latency_ms = int((time.monotonic() - started) * 1000)
            return _hold("LLM_UNKNOWN", latency_ms=latency_ms, usage=usage_acc)

    latency_ms = int((time.monotonic() - started) * 1000)
    return _hold(last_error_code, latency_ms=latency_ms, usage=usage_acc)
