"""PeCal Pulse agent-workflow settings, read from the environment / root .env.

Secrets are SecretStr and are never logged or returned by any endpoint.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class PulseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # Gemini (Google Gen AI SDK)
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.5-flash"
    gemini_timeout_seconds: float = Field(default=40.0, gt=0, le=180)
    gemini_max_attempts: int = Field(default=3, ge=1, le=5)

    # n8n
    n8n_base_url: str = "http://127.0.0.1:5678"
    n8n_execute_path: str = "/webhook/pulse-execute"
    n8n_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    pulse_webhook_secret: SecretStr = SecretStr("")
    pulse_callback_base_url: str = "http://127.0.0.1:8001"
    webhook_max_skew_seconds: int = Field(default=300, ge=30, le=3600, validation_alias=AliasChoices("PULSE_WEBHOOK_MAX_SKEW_SECONDS"))

    # Security / approvals
    pulse_session_secret: SecretStr = SecretStr("")
    pulse_auth_mode: Literal["demo", "password"] = "demo"
    pulse_demo_mode: bool = True
    approval_ttl_seconds: int = Field(default=900, ge=10, le=86400, validation_alias=AliasChoices("PULSE_APPROVAL_TTL_SECONDS"))
    auto_approve_low_risk: bool = Field(default=True, validation_alias=AliasChoices("PULSE_AUTO_APPROVE_LOW_RISK"))
    allow_external_email: bool = Field(default=False, validation_alias=AliasChoices("PULSE_ALLOW_EXTERNAL_EMAIL"))

    # Storage — business records and graph checkpoints are separate files.
    pulse_db_path: str = "data/runtime/pulse.sqlite3"
    pulse_checkpoint_path: str = "data/runtime/pulse-checkpoints.sqlite3"
    pulse_snapshot: str = ""
    node_timeout_seconds: float = Field(default=60.0, gt=0, le=600, validation_alias=AliasChoices("PULSE_NODE_TIMEOUT_SECONDS"))

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else ROOT / p

    @property
    def db_file(self) -> Path:
        return self.path(self.pulse_db_path)

    @property
    def checkpoint_file(self) -> Path:
        return self.path(self.pulse_checkpoint_path)

    @property
    def snapshot_id(self) -> str:
        return self.pulse_snapshot or os.getenv("PECAL_SNAPSHOT", "synthetic-v1")

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key.get_secret_value().strip())

    def webhook_secret(self) -> bytes:
        value = self.pulse_webhook_secret.get_secret_value()
        if not value:
            raise RuntimeError("PULSE_WEBHOOK_SECRET is not configured")
        return value.encode()

    def session_secret(self) -> bytes:
        value = self.pulse_session_secret.get_secret_value()
        return (value or _ephemeral_secret()).encode()


@lru_cache(maxsize=1)
def _ephemeral_secret() -> str:
    # Without a configured secret, sessions survive only this process.
    return secrets.token_hex(32)


@lru_cache(maxsize=1)
def get_settings() -> PulseSettings:
    return PulseSettings()


def reset_settings() -> None:
    get_settings.cache_clear()
