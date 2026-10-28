"""Firebase auth gate for the standalone Quarterdeck site.

The hosted room uses the same Firebase project as app.eyry.io (Google sign-in)
and maps every signed-in visitor to the single ``operator`` identity. There are
no separate Quarterdeck accounts.

Server-side enforcement lives in :class:`AuthGate`, a small ASGI middleware that
validates the Firebase ID token on both HTTP and websocket connections. Tokens
arrive as ``Authorization: Bearer <id-token>`` (API clients) or in the HttpOnly
session cookie set by ``POST /api/session`` (browser + websocket). Token
verification is the same RS256/JWKS check Eyry Pro performs for its embedded
room; see ``eyry_pro/auth.py``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

# Public client config for the existing Eyry Pro Firebase project. These are
# the values served to app.eyry.io's frontend; they are safe to expose. A real
# server-side secret is never needed: ID tokens are verified against Google's
# public JWKS using the project id as the audience.
DEFAULT_FIREBASE_PROJECT_ID = "eyry-pro-web-2026"
DEFAULT_FIREBASE_AUTH_DOMAIN = "eyry-pro-web-2026.firebaseapp.com"
DEFAULT_FIREBASE_API_KEY = "AIzaSyAbyZngRWwoLb4ZXcmWaMSTiQC7idhWYO0"

_JWKS_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/"
    "securetoken@system.gserviceaccount.com"
)
_CACHE_TTL = 3600

_IDENTITY_ASSETS = {
    "/favicon.ico",
    "/favicon.svg",
    "/favicon-16.png",
    "/favicon-32.png",
    "/favicon-48.png",
    "/apple-touch-icon.png",
    "/icon-192.png",
    "/icon-512.png",
    "/icon-maskable-512.png",
    "/og-image.png",
    "/manifest.webmanifest",
    "/manifest.json",
    "/site.webmanifest",
    "/sw.js",
}


class AuthError(Exception):
    """Raised when a token is missing, malformed, expired, or not the operator."""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_value(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


class FirebaseAuth:
    """Verify Firebase ID tokens against Google's public JWKS."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self._keys: dict[str, str] = {}
        self._keys_exp = 0.0

    def _refresh_keys(self) -> None:
        import httpx

        resp = httpx.get(_JWKS_URL, timeout=10)
        resp.raise_for_status()
        self._keys = resp.json()
        cache_control = resp.headers.get("cache-control", "")
        max_age = 3600
        for part in cache_control.split(","):
            part = part.strip()
            if part.startswith("max-age="):
                try:
                    max_age = int(part.split("=", 1)[1])
                except ValueError:
                    pass
        self._keys_exp = time.time() + min(max_age, _CACHE_TTL)

    def _get_key(self, kid: str) -> str:
        if time.time() >= self._keys_exp or kid not in self._keys:
            self._refresh_keys()
        try:
            cert_pem = self._keys[kid]
        except KeyError:
            raise AuthError("unknown signing key") from None
        try:
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives import serialization

            cert = x509.load_pem_x509_certificate(
                cert_pem.encode(), default_backend())
            return cert.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode()
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(f"bad signing key: {exc}") from exc

    def verify(self, token: str) -> dict:
        """Return claims for a valid token, or raise :class:`AuthError`."""
        if not self.project_id:
            raise AuthError("FIREBASE_PROJECT_ID not configured")
        import jwt

        try:
            kid = jwt.get_unverified_header(token).get("kid", "")
        except Exception:
            raise AuthError("malformed token") from None
        key = self._get_key(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.project_id,
                issuer=f"https://securetoken.google.com/{self.project_id}",
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise AuthError("token expired") from None
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"invalid token: {exc}") from exc
        return {
            "uid": claims["sub"],
            "email": claims.get("email", ""),
            "verified": bool(claims.get("email_verified", False)),
        }


@dataclass
class AuthSettings:
    """Runtime auth configuration, read from the service environment."""

    required: bool = False
    project_id: str = DEFAULT_FIREBASE_PROJECT_ID
    auth_domain: str = DEFAULT_FIREBASE_AUTH_DOMAIN
    api_key: str = DEFAULT_FIREBASE_API_KEY
    operator_uid: str = ""
    public_url: str = "https://quarterdeck.eyry.io"
    cookie_name: str = "qd_session"
    cookie_max_age: int = 3600
    # Test hook: an object with verify(token) -> claims. Production leaves this
    # unset and gets FirebaseAuth(project_id).
    verifier: Any | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "AuthSettings":
        return cls(
            required=_env_flag("QD_AUTH_REQUIRED"),
            project_id=_env_value(
                "QD_FIREBASE_PROJECT_ID", "FIREBASE_PROJECT_ID",
            ) or DEFAULT_FIREBASE_PROJECT_ID,
            auth_domain=_env_value(
                "QD_FIREBASE_AUTH_DOMAIN", "FIREBASE_AUTH_DOMAIN",
            ) or DEFAULT_FIREBASE_AUTH_DOMAIN,
            api_key=_env_value(
                "QD_FIREBASE_API_KEY", "FIREBASE_API_KEY",
            ) or DEFAULT_FIREBASE_API_KEY,
            operator_uid=_env_value("QD_OPERATOR_UID", "QD_OWNER_UID"),
            public_url=_env_value("QD_PUBLIC_URL") or "https://quarterdeck.eyry.io",
        )

    def build_verifier(self):
        if self.verifier is not None:
            return self.verifier
        return FirebaseAuth(self.project_id)


class AuthGate:
    """ASGI middleware enforcing Firebase auth on HTTP API and websockets."""

    _PUBLIC_PATHS = {
        "/health",
        "/api/config",
        "/api/session",
        "/login",
    }

    def __init__(self, app, settings: AuthSettings, *,
                 login_page: Path | None = None):
        self.app = app
        self.settings = settings
        self.verifier = settings.build_verifier() if settings.required else None
        self.login_page = login_page
        self._login_html: bytes | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.required)

    def verify(self, token: str) -> dict:
        if self.verifier is None:
            raise AuthError("authentication is not configured")
        try:
            claims = self.verifier.verify(token)
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(f"token verification failed: {exc}") from exc
        uid = str(claims.get("uid") or claims.get("sub") or "").strip()
        if not uid:
            raise AuthError("token has no subject")
        operator = (self.settings.operator_uid or "").strip()
        if operator and uid != operator:
            raise AuthError("not the operator account")
        return {
            "uid": uid,
            "email": claims.get("email", ""),
            "verified": bool(claims.get("verified",
                                       claims.get("email_verified", False))),
            "name": "operator",
            "auth_method": "firebase",
        }

    def _token_from_scope(self, scope: dict, *, allow_query: bool) -> str:
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        authorization = headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[len("Bearer "):].strip()
            if token:
                return token
        raw_cookie = headers.get("cookie", "")
        if raw_cookie:
            jar = SimpleCookie()
            try:
                jar.load(raw_cookie)
            except Exception:
                jar = SimpleCookie()
            morsel = jar.get(self.settings.cookie_name)
            if morsel is not None and morsel.value:
                return morsel.value
        if allow_query:
            params = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            token = (params.get("token") or [""])[0].strip()
            if token:
                return token
        return ""

    def claims_from_scope(self, scope: dict) -> dict | None:
        if not self.enabled:
            return None
        token = self._token_from_scope(
            scope, allow_query=scope.get("type") == "websocket")
        if not token:
            return None
        try:
            return self.verify(token)
        except AuthError:
            return None

    def _is_public(self, path: str) -> bool:
        return (
            path in self._PUBLIC_PATHS
            or path in _IDENTITY_ASSETS
            or path.startswith("/static/")
        )

    def _login_bytes(self) -> bytes:
        if self._login_html is None:
            try:
                self._login_html = self.login_page.read_bytes()
            except (AttributeError, OSError):
                self._login_html = (
                    b"<!doctype html><meta charset=utf-8>"
                    b"<title>Quarterdeck</title>"
                    b"<h1>Sign in required</h1>"
                )
        return self._login_html

    async def _send_login(self, send) -> None:
        body = self._login_bytes()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
                (b"referrer-policy", b"no-referrer"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _send_unauthorized(self, send) -> None:
        body = b'{"detail":"authentication required"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _close_websocket(self, send) -> None:
        await send({
            "type": "websocket.close",
            "code": 1008,
            "reason": "authentication required",
        })

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") not in ("http", "websocket") or not self.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        claims = self.claims_from_scope(scope)
        if claims is not None:
            scope.setdefault("state", {})["user"] = claims

        if scope["type"] == "websocket":
            if path.startswith("/ws/") and claims is None:
                await self._close_websocket(send)
                return
            await self.app(scope, receive, send)
            return

        if self._is_public(path):
            await self.app(scope, receive, send)
            return
        if claims is not None:
            await self.app(scope, receive, send)
            return
        if path in ("/", "/agents/room", "/login"):
            await self._send_login(send)
            return
        await self._send_unauthorized(send)
