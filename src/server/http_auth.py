"""Bearer-token gate for the streamable-http transport.

Each caller gets their own token. The server stores only SHA-256 hashes of
those tokens, in a JSON file that maps a caller name to a hash:

    {"andrew": "3f1c...", "adrienne": "9ab2..."}

Generate a token and its hash with ``keep-mcp token <name>``.
"""

import contextvars
import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path

logger = logging.getLogger("keep_mcp.auth")

# Name of the caller for the current request, for audit logging.
current_caller: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "keep_mcp_caller", default=None
)


class TokenMapError(ValueError):
    """The token-map file is malformed."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str]:
    """Return a fresh (token, sha256 hex digest) pair."""
    token = "kmcp_" + secrets.token_urlsafe(32)
    return token, hash_token(token)


def load_token_map(path: str | Path) -> dict[str, str]:
    """Load ``{caller name: sha256 hex}`` and return ``{sha256 hex: caller name}``."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise TokenMapError(f"{path}: expected a non-empty JSON object of name -> sha256 hex")
    by_hash: dict[str, str] = {}
    for name, digest in raw.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise TokenMapError(f"{path}: names and hashes must be strings")
        digest = digest.strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise TokenMapError(f"{path}: hash for {name!r} is not a SHA-256 hex digest")
        if digest in by_hash:
            raise TokenMapError(f"{path}: {name!r} and {by_hash[digest]!r} share a token")
        by_hash[digest] = name
    return by_hash


class BearerAuthMiddleware:
    """ASGI middleware that requires ``Authorization: Bearer <token>`` on every HTTP request."""

    def __init__(self, app, token_hashes: dict[str, str], realm: str = "keep-mcp"):
        if not token_hashes:
            raise ValueError("BearerAuthMiddleware needs at least one token")
        self.app = app
        self.token_hashes = token_hashes
        self.realm = realm

    def _caller_for(self, token: str) -> str | None:
        digest = hash_token(token)
        match = None
        # Compare against every entry in constant time so timing does not leak which hash matched.
        for known, name in self.token_hashes.items():
            if hmac.compare_digest(known, digest):
                match = name
        return match

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header = b""
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                header = value
                break
        scheme, _, token = header.decode("latin-1").partition(" ")
        caller = self._caller_for(token.strip()) if scheme.lower() == "bearer" and token else None

        if caller is None:
            body = b'{"error":"unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", f'Bearer realm="{self.realm}"'.encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        if scope.get("method") == "POST":
            logger.info("request from %s", caller)
        reset = current_caller.set(caller)
        try:
            await self.app(scope, receive, send)
        finally:
            current_caller.reset(reset)
