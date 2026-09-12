"""Bearer auth over the configured API keys (constant-time compare)."""

import hmac


def authenticate(authorization: str | None, keys: dict[str, str]) -> str | None:
    """Return the caller name for a valid Authorization header, else None."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    presented = parts[1].strip()
    if not presented:
        return None
    for key, name in keys.items():
        if hmac.compare_digest(presented.encode(), key.encode()):
            return name
    return None
