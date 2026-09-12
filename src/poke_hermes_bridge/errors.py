"""Error envelope and bridge-level exceptions."""

from typing import Any


class BridgeError(Exception):
    """An error rendered as the bridge error envelope."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def envelope(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}
