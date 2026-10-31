"""Checks the Entra External ID access token the browser sends as "Authorization: Bearer <token>".

The browser signs in with MSAL.js; this side only verifies: signature (Entra's published keys, cached 24 h),
issuer, audience, expiry and the practice.readwrite scope. Tokens are never stored or logged.
"""
from functools import lru_cache

import httpx
import jwt

from .config import settings


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@lru_cache(maxsize=1)
def _jwks_url() -> str:
    if settings.auth_jwks_url:
        return settings.auth_jwks_url
    # Find the key set from the issuer's OpenID configuration.
    response = httpx.get(settings.auth_issuer.rstrip("/") + "/.well-known/openid-configuration", timeout=10)
    response.raise_for_status()
    return response.json()["jwks_uri"]


@lru_cache(maxsize=1)
def _jwks() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(_jwks_url(), cache_keys=True, lifespan=86400, timeout=10)


def signing_key(token: str):
    return _jwks().get_signing_key_from_jwt(token).key


def verify(token: str) -> dict:
    """The token's claims, or AuthError (401 bad token, 403 missing scope, 503 keys unreachable)."""
    try:
        key = signing_key(token)
    except (jwt.PyJWKClientConnectionError, httpx.HTTPError):
        raise AuthError(503, "Sign-in check is unavailable right now. Try again in a minute.")
    except (jwt.PyJWKClientError, jwt.InvalidTokenError):  # malformed token, or a key Entra never published
        raise AuthError(401, "Please sign in again.")
    audiences = [a.strip() for a in settings.auth_audience.split(",") if a.strip()]
    try:
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=audiences or None,
                            issuer=settings.auth_issuer, leeway=60,
                            options={"require": ["exp", "iat", "sub", "iss"], "verify_aud": bool(audiences)})
    except jwt.InvalidTokenError:
        raise AuthError(401, "Please sign in again.")
    if settings.auth_scope and settings.auth_scope not in str(claims.get("scp", "")).split():
        raise AuthError(403, "This sign-in can't use Bol. Sign out and sign in again.")
    return claims


def identity(claims: dict) -> tuple[str, str | None, str]:
    """(subject, email, display name) from verified claims."""
    email = claims.get("email") or claims.get("preferred_username")
    if isinstance(email, list):  # some providers send a list of emails
        email = email[0] if email else None
    name = str(claims.get("given_name") or claims.get("name") or "").strip()
    if name.lower() == "unknown":
        name = ""
    return str(claims["sub"]), (str(email)[:200] if email else None), name.split(" ")[0][:80]
