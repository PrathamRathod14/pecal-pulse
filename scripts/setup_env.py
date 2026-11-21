#!/usr/bin/env python3
"""Create/complete the local .env: copies .env.example and fills empty secrets.

Never overwrites a value you already set, never prints secrets.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATED = {
    "PULSE_WEBHOOK_SECRET": lambda: secrets.token_hex(32),
    "PULSE_SESSION_SECRET": lambda: secrets.token_hex(32),
    "N8N_ENCRYPTION_KEY": lambda: secrets.token_hex(24),
}


def main() -> int:
    env, example = ROOT / ".env", ROOT / ".env.example"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else example.read_text(encoding="utf-8").splitlines()
    present = {line.split("=", 1)[0].strip() for line in lines if "=" in line and not line.lstrip().startswith("#")}
    # Add keys introduced in .env.example since the .env was created.
    for line in example.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key not in present:
                lines.append(line)
                present.add(key)
    filled = []
    out = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            key = key.strip()
            if key in GENERATED and not value.strip().strip("'\""):
                line = f"{key}={GENERATED[key]()}"
                filled.append(key)
        out.append(line)
    env.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        env.chmod(0o600)
    except OSError:
        pass
    print(f".env ready ({'generated ' + ', '.join(filled) if filled else 'no secrets needed generating'})")
    missing = [k for k in ("GEMINI_API_KEY",) if not any(l.startswith(k + "=") and l.split("=", 1)[1].strip() for l in out)]
    if missing:
        print("Optional credentials not set: " + ", ".join(missing) + " (labelled fallbacks will be used)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
