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


def _get_client():
    global _client, _client_key
    settings = get_settings()
    key = settings.gemini_api_key.get_secret_value().strip()
    if not key:
        raise LLMUnavailable("not_configured", "GEMINI_API_KEY is not set")
    if _client is None or _client_key != key:
        from google import genai

        _client = genai.Client(api_key=key)
        _client_key = key
    return _client


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
                client.aio.models.generate_content(model=settings.gemini_model, contents=prompt, config=config),
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
                "model": settings.gemini_model,
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
