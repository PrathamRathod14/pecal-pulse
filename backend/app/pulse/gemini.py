"""Gemini via the official Google Gen AI SDK (``google-genai``).

Structured output: the response is constrained to a JSON schema and then
validated again with Pydantic. Rate limits / 5xx are retried with backoff;
timeouts, malformed JSON and missing credentials raise ``LLMUnavailable``
with a safe category (never the raw provider body or key).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random

from pydantic import BaseModel, ValidationError

from .config import get_settings

log = logging.getLogger("pulse.gemini")


class LLMUnavailable(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


_client = None
_client_key = None
_mode: str | None = None  # "developer" (AI Studio) or "vertex" (Vertex AI express mode)
probe_result: dict = {"checked": False}
_effective_model: str | None = None
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-flash-latest", "gemini-3.8-flash"]
RETIRED_MODELS = {"gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"}  # closed to new Gemini API users
_last_probe: float = 0.0


def preferred_model() -> str:
    configured = get_settings().gemini_model
    return FALLBACK_MODELS[0] if configured in RETIRED_MODELS else configured


def current_model() -> str:
    return _effective_model or preferred_model()


def needs_reprobe(every: float = 120.0) -> bool:
    import time
    return get_settings().gemini_configured and not probe_result.get("ok") and time.time() - _last_probe > every


def _make_client(key: str, mode: str):
    from google import genai

    return genai.Client(vertexai=True, api_key=key) if mode == "vertex" else genai.Client(api_key=key)


def _get_client():
    global _client, _client_key
    settings = get_settings()
    key = settings.gemini_api_key.get_secret_value().strip()
    if not key:
        raise LLMUnavailable("not_configured", "GEMINI_API_KEY is not set")
    if _client is None or _client_key != key:
        _client = _make_client(key, _mode or "developer")
        _client_key = key
    return _client


def current_mode() -> str:
    return _mode or "developer"


async def probe(timeout: float = 40.0) -> dict:
    """One tiny request at startup to verify the key and pick the endpoint.

    AI Studio keys use the Gemini Developer API; keys created for Vertex AI
    express mode (often starting with "AQ.") need vertexai=True. Never logs the key.
    """
    global _client, _client_key, _mode, probe_result
    settings = get_settings()
    key = settings.gemini_api_key.get_secret_value().strip()
    if not key:
        probe_result = {"checked": True, "ok": False, "reason": "GEMINI_API_KEY is not set"}
        return probe_result
    global _effective_model, _last_probe
    import time
    _last_probe = time.time()
    errors_seen = []
    modes = ["developer", "vertex"]  # AI Studio keys (including "AQ." keys) use the Gemini API
    models = list(dict.fromkeys([preferred_model(), *FALLBACK_MODELS]))
    for mode in modes:
        client = _make_client(key, mode)
        for model in models:
          for attempt in (1, 2):  # one retry for transient 503 "high demand"
            try:
                r = await asyncio.wait_for(client.aio.models.generate_content(
                    model=model, contents="Reply with the single word OK."), timeout=timeout)
                if (r.text or "").strip():
                    _client, _client_key, _mode, _effective_model = client, key, mode, model
                    probe_result = {"checked": True, "ok": True, "mode": mode, "model": model}
                    return probe_result
                break
            except asyncio.TimeoutError:
                errors_seen.append(f"{mode}/{model}: timeout")
                break
            except Exception as exc:  # noqa: BLE001 - category only, never the body
                code = getattr(exc, "code", None)
                errors_seen.append(f"{mode}/{model}: {type(exc).__name__}{f' HTTP {code}' if code else ''}")
                if code in (500, 502, 503, 504) and attempt == 1:
                    await asyncio.sleep(3)
                    continue
                break
          if errors_seen and errors_seen[-1].startswith(f"{mode}/") and (" HTTP 401" in errors_seen[-1] or " HTTP 403" in errors_seen[-1]):
              break  # key not valid for this endpoint: skip its other models
    probe_result = {"checked": True, "ok": False, "reason": "Gemini rejected the key or is unreachable (" + "; ".join(errors_seen) + ")"}
    return probe_result


async def generate_structured(system: str, prompt: str, schema: type[BaseModel], *, temperature: float = 0.2) -> tuple[BaseModel, dict]:
    """Returns (validated model, metadata). Raises LLMUnavailable."""
    from google.genai import errors, types

    settings = get_settings()
    client = _get_client()
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=temperature,
    )
    last: Exception | None = None
    for attempt in range(1, settings.gemini_max_attempts + 1):
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(model=current_model(), contents=prompt, config=config),
                timeout=settings.gemini_timeout_seconds,
            )
            text = response.text or ""
            try:
                parsed = schema.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                last = LLMUnavailable("malformed_response", f"Gemini returned output that failed validation ({type(exc).__name__})")
                continue  # retry once more with the same prompt
            usage = getattr(response, "usage_metadata", None)
            return parsed, {
                "model": current_model(),
                "attempts": attempt,
                "input_tokens": getattr(usage, "prompt_token_count", None),
                "output_tokens": getattr(usage, "candidates_token_count", None),
            }
        except asyncio.TimeoutError:
            last = LLMUnavailable("timeout", f"Gemini did not respond within {settings.gemini_timeout_seconds:.0f}s")
        except errors.APIError as exc:
            code = getattr(exc, "code", None)
            if code in (429, 500, 502, 503, 504):
                last = LLMUnavailable("rate_limited" if code == 429 else "provider_unavailable", f"Gemini returned HTTP {code}")
                await asyncio.sleep(min(8, 2 ** attempt) + random.random())
                continue
            category = {400: "bad_request", 401: "auth", 403: "auth", 404: "model_not_found"}.get(code, "provider_error")
            raise LLMUnavailable(category, f"Gemini request failed (HTTP {code})")
        except LLMUnavailable:
            raise
        except Exception as exc:  # network errors etc.
            log.warning("Gemini call failed: %s", type(exc).__name__)
            last = LLMUnavailable("network", f"Gemini request failed ({type(exc).__name__})")
            await asyncio.sleep(1)
    assert last is not None
    raise last if isinstance(last, LLMUnavailable) else LLMUnavailable("unknown", str(last))
