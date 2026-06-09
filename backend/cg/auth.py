"""Tiny cookie-session auth for the public demo deployment."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import HTTPException, Request, Response, status

from cg.settings import Settings, get_settings


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _signature(body: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()


def create_session_token(username: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + settings.cg_auth_session_ttl_seconds,
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_signature(body, settings.cg_auth_secret)}"


def parse_session_token(token: str | None, settings: Settings | None = None) -> str | None:
    if not token or "." not in token:
        return None
    settings = settings or get_settings()
    body, given_signature = token.rsplit(".", 1)
    expected_signature = _signature(body, settings.cg_auth_secret)
    if not hmac.compare_digest(given_signature, expected_signature):
        return None
    try:
        payload: dict[str, Any] = json.loads(_b64decode(body))
    except Exception:
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    username = str(payload.get("sub") or "")
    if username != settings.cg_auth_username:
        return None
    return username


def session_user_from_request(request: Request, settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    return parse_session_token(request.cookies.get(settings.cg_auth_cookie_name), settings)


def require_session_user(request: Request) -> str:
    username = session_user_from_request(request)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return username


def set_session_cookie(response: Response, username: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    response.set_cookie(
        key=settings.cg_auth_cookie_name,
        value=create_session_token(username, settings),
        max_age=settings.cg_auth_session_ttl_seconds,
        httponly=True,
        secure=False,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    response.delete_cookie(key=settings.cg_auth_cookie_name, path="/", samesite="lax")

