"""Actor identity, roles and least-privilege permissions.

Roles are resolved server-side from the user directory; a role sent by the
browser is never read. Sessions are HMAC-signed, expiring, httpOnly cookies.

``demo`` auth mode (default for local use) lets you pick one of the demo
identities without a password — it is labelled as such in the UI and must
not be used outside a local demo. ``password`` mode requires
PULSE_USER_<ID>_PASSWORD (scrypt-hashed in memory) for each user.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from .config import get_settings

SESSION_COOKIE = "pulse_session"
SESSION_TTL = 8 * 3600

PERMISSIONS: dict[str, set[str]] = {
    "viewer": {"pulse:read"},
    "analyst": {"pulse:read", "investigation:start", "audit:read"},
    "sales_manager": {
        "pulse:read", "investigation:start", "audit:read", "security:read",
        "action:approve:standard", "execution:retry",
    },
    "director": {
        "pulse:read", "investigation:start", "audit:read", "security:read",
        "action:approve:standard", "action:approve:financial", "execution:retry",
        "security:probe",
    },
}
ROLE_RANK = {"viewer": 0, "analyst": 1, "sales_manager": 2, "director": 3}


@dataclass(frozen=True)
class Actor:
    id: str
    name: str
    role: str
    kind: str = "human"  # human | agent | service
    permissions: frozenset[str] = field(default_factory=frozenset)

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "role": self.role, "kind": self.kind,
                "permissions": sorted(self.permissions)}


DEMO_USERS = {
    "maria.analyst": ("Maria Keller", "analyst"),
    "jonas.manager": ("Jonas Weber", "sales_manager"),
    "elena.director": ("Elena Fischer", "director"),
    "sam.viewer": ("Sam Becker", "viewer"),
}


def _user(user_id: str) -> Actor | None:
    entry = DEMO_USERS.get(user_id)
    if not entry:
        return None
    name, role = entry
    return Actor(id=user_id, name=name, role=role, permissions=frozenset(PERMISSIONS[role]))


def agent_actor(agent: str) -> Actor:
    """Agents hold no approval or execution permission at all."""
    return Actor(id=f"agent:{agent}", name=agent, role="agent", kind="agent", permissions=frozenset())


SERVICE_N8N = Actor(id="service:n8n", name="n8n", role="service", kind="service",
                    permissions=frozenset({"callback:submit", "signal:scan"}))
SYSTEM = Actor(id="system:pulse", name="PulseGuard", role="system", kind="service", permissions=frozenset())


def list_users() -> list[dict]:
    return [_user(uid).public() for uid in DEMO_USERS]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_session(user_id: str) -> str:
    body = _b64(json.dumps({"sub": user_id, "exp": int(time.time()) + SESSION_TTL,
                            "nonce": secrets.token_hex(8)}).encode())
    sig = _b64(hmac.new(get_settings().session_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_session(token: str | None) -> Actor | None:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = _b64(hmac.new(get_settings().session_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        claims = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(claims.get("exp", 0)) < time.time():
        return None
    return _user(str(claims.get("sub", "")))


def check_password(user_id: str, password: str | None) -> bool:
    settings = get_settings()
    if settings.pulse_auth_mode == "demo":
        return user_id in DEMO_USERS
    configured = os.getenv(f"PULSE_USER_{user_id.replace('.', '_').upper()}_PASSWORD", "")
    if not configured or not password:
        return False
    salt = b"pecal-pulse"
    a = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    b = hashlib.scrypt(configured.encode(), salt=salt, n=2**14, r=8, p=1)
    return hmac.compare_digest(a, b)


def current_actor(request: Request) -> Actor:
    actor = read_session(request.cookies.get(SESSION_COOKIE))
    if actor is None:
        raise HTTPException(401, "Sign in to PeCal Pulse to continue")
    return actor


def require(actor: Actor, permission: str) -> None:
    if not actor.can(permission):
        raise HTTPException(403, f"Role '{actor.role}' lacks permission '{permission}'")


def require_csrf(request: Request) -> None:
    # SameSite=Strict cookie + a custom header a cross-site form cannot set.
    if request.headers.get("x-pulse-request") != "1":
        raise HTTPException(403, "Missing X-Pulse-Request header")
