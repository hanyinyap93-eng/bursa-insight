"""Server-side auth for the gated pages (GEX / Market Index / R-Appetite).

Flow: the browser signs in with Google Identity Services and POSTs the resulting
Google ID token to /api/auth/google. We verify that token against Google's
public keys (RS256, correct audience + issuer + not expired) and, only if it is
genuine, mint our OWN short-lived session JWT (HS256, signed with settings.jwt_secret).
The gated routes then require that session JWT via the `require_auth` dependency.

This replaces the previous client-only gate, where the endpoints were fully open
and the browser merely hid the nav links.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from fastapi import Header, HTTPException
from jose import jwt
from jose.exceptions import JWTError

from ..config import settings

# Google's rotating public keys (JWK set). Cached for an hour; refreshed on a
# key-id miss (Google rotates keys, so an unknown kid may just mean "refresh").
_GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
_jwks: dict[str, Any] = {"keys": None, "exp": 0.0}


def _google_jwks(force: bool = False) -> list[dict]:
    now = time.time()
    if not force and _jwks["keys"] and now < _jwks["exp"]:
        return _jwks["keys"]
    r = httpx.get(_GOOGLE_CERTS_URL, timeout=10)
    r.raise_for_status()
    _jwks["keys"] = r.json()["keys"]
    _jwks["exp"] = now + 3600
    return _jwks["keys"]


def _select_key(kid: str) -> Optional[dict]:
    key = next((k for k in _google_jwks() if k.get("kid") == kid), None)
    if key is None:  # unknown kid -> keys may have rotated; refresh once
        key = next((k for k in _google_jwks(force=True) if k.get("kid") == kid), None)
    return key


def verify_google_idtoken(token: str) -> dict:
    """Verify a Google ID token and return its claims, or raise ValueError."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except JWTError as exc:
        raise ValueError(f"malformed token: {exc}") from exc
    key = _select_key(kid or "")
    if key is None:
        raise ValueError("token key id not found in Google's key set")
    try:
        # audience is checked here; issuer is checked manually (Google uses two)
        claims = jwt.decode(
            token, key, algorithms=["RS256"],
            audience=settings.google_client_id,
            options={"verify_iss": False},
        )
    except JWTError as exc:
        raise ValueError(f"token verification failed: {exc}") from exc
    if claims.get("iss") not in _GOOGLE_ISSUERS:
        raise ValueError("unexpected token issuer")
    if not claims.get("email"):
        raise ValueError("token has no email")
    if claims.get("email_verified") is False:
        raise ValueError("email not verified by Google")
    return claims


def make_session_jwt(email: str, name: str = "") -> dict:
    """Mint our own session token after a successful Google verification."""
    now = int(time.time())
    exp = now + settings.jwt_ttl_hours * 3600
    token = jwt.encode(
        {"sub": email, "name": name, "iat": now, "exp": exp},
        settings.jwt_secret, algorithm="HS256",
    )
    return {"token": token, "email": email, "name": name, "exp": exp}


def make_verify_token(email: str) -> str:
    """A short-lived token embedded in the email-confirmation link. Purpose-tagged
    so it can't be used as a session token and vice-versa."""
    now = int(time.time())
    return jwt.encode(
        {"sub": email, "purpose": "verify", "iat": now,
         "exp": now + settings.verify_ttl_hours * 3600},
        settings.jwt_secret, algorithm="HS256")


def read_verify_token(token: str) -> str:
    """Return the email from a valid confirmation token, or raise ValueError."""
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise ValueError("this confirmation link is invalid or has expired") from exc
    if claims.get("purpose") != "verify" or not claims.get("sub"):
        raise ValueError("this confirmation link is invalid")
    return claims["sub"]


def _verify_session_jwt(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])


def require_auth(authorization: str = Header(None)) -> dict:
    """FastAPI dependency: 401 unless a valid, unexpired session JWT is present
    as `Authorization: Bearer <token>`. Returns the token claims on success."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "authentication required")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return _verify_session_jwt(token)
    except JWTError as exc:
        raise HTTPException(401, f"invalid or expired session: {exc}") from exc
