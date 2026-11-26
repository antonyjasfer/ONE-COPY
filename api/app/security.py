from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlparse
from uuid import UUID

from fastapi import HTTPException, Request, Response, status

from app.config import Settings


def sign_visitor_id(visitor_id: UUID, secret: str) -> str:
    payload = str(visitor_id)
    sig = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{sig}"


def verify_visitor_cookie(cookie_value: str | None, secret: str) -> UUID | None:
    if not cookie_value or "." not in cookie_value:
        return None
    payload, sig = cookie_value.split(".", 1)
    expected_sig = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        return UUID(payload)
    except ValueError:
        return None


def set_visitor_cookie(response: Response, visitor_id: UUID, settings: Settings) -> None:
    response.set_cookie(
        key=settings.cookie_name,
        value=sign_visitor_id(visitor_id, settings.cookie_secret),
        max_age=settings.cookie_ttl_seconds,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
    )


def get_verified_visitor_id(request: Request, settings: Settings) -> UUID:
    cookie_val = request.cookies.get(settings.cookie_name)
    visitor_id = verify_visitor_cookie(cookie_val, settings.cookie_secret)
    if visitor_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid visitor session cookie.",
        )
    return visitor_id


def _extract_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return origin.strip().rstrip("/").lower()
    referer = request.headers.get("referer")
    if referer:
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".lower()
    return None


def validate_origin(request: Request, settings: Settings) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = _extract_origin(request)
    if origin is None:
        if settings.is_production:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Missing request origin.",
            )
        return
    if origin not in settings.allowed_origins:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request origin is not allowed.",
        )
