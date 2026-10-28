"""Standalone auth gate: Firebase sign-in, operator identity, API/ws enforcement."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quarterdeck.auth import AuthError, AuthSettings
from quarterdeck.web import create_app

TOKEN = "test-id-token"
OPERATOR_UID = "operator-uid"


class FakeFirebase:
    def __init__(self, uid: str = OPERATOR_UID):
        self.uid = uid

    def verify(self, token: str) -> dict:
        if token != TOKEN:
            raise AuthError("invalid token")
        return {
            "uid": self.uid,
            "email": "operator@example.com",
            "verified": True,
        }


def _settings(uid: str = OPERATOR_UID) -> AuthSettings:
    return AuthSettings(
        required=True,
        project_id="test-project",
        auth_domain="test-project.firebaseapp.com",
        api_key="test-api-key",
        operator_uid=OPERATOR_UID,
        verifier=FakeFirebase(uid),
    )


def _bearer(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(home=tmp_path, auth=_settings())
    with TestClient(app) as test_client:
        yield test_client


class TestLoginPage:
    def test_anonymous_root_serves_login(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Continue with Google" in r.text
        assert 'id="messages"' not in r.text

    def test_authenticated_root_serves_room(self, client):
        r = client.get("/", headers=_bearer())
        assert r.status_code == 200
        assert 'id="messages"' in r.text
        assert "Continue with Google" not in r.text

    def test_public_config_exposes_only_client_values(self, client):
        r = client.get("/api/config")
        assert r.status_code == 200
        assert r.json() == {
            "auth_required": True,
            "firebase_project_id": "test-project",
            "firebase_auth_domain": "test-project.firebaseapp.com",
            "firebase_api_key": "test-api-key",
            "site_url": "https://quarterdeck.eyry.io",
        }

    def test_health_stays_public(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True


class TestPwaAssets:
    def test_install_assets_stay_public(self, client):
        for path in (
            "/manifest.webmanifest",
            "/sw.js",
            "/offline.html",
            "/icon-192.png",
            "/icon-maskable-192.png",
            "/apple-touch-icon.png",
        ):
            r = client.get(path)
            assert r.status_code == 200, path


class TestApiEnforcement:
    def test_anonymous_api_is_rejected(self, client):
        r = client.get("/api/channels")
        assert r.status_code == 401
        assert r.json()["detail"] == "authentication required"

    def test_bad_token_is_rejected(self, client):
        assert client.get("/api/channels", headers=_bearer("nope")).status_code == 401

    def test_valid_token_is_accepted(self, client):
        r = client.get("/api/channels", headers=_bearer())
        assert r.status_code == 200
        assert r.json() == {"channels": []}

    def test_session_cookie_authenticates_browser(self, client):
        r = client.post("/api/session", headers=_bearer())
        assert r.status_code == 200
        assert r.json()["name"] == "operator"
        assert client.cookies.get("qd_session") == TOKEN
        assert client.get("/api/channels").status_code == 200
        assert 'id="messages"' in client.get("/").text

    def test_session_rejects_non_operator(self, tmp_path):
        app = create_app(home=tmp_path, auth=_settings(uid="someone-else"))
        with TestClient(app) as other:
            r = other.post("/api/session", headers=_bearer())
            assert r.status_code == 401
            assert other.get("/api/channels", headers=_bearer()).status_code == 401

    def test_forwarded_https_sets_secure_cookie(self, client):
        r = client.post(
            "/api/session",
            headers={**_bearer(), "X-Forwarded-Proto": "https"},
        )
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert "secure" in cookie
        assert "httponly" in cookie
        assert "samesite=lax" in cookie

    def test_logout_clears_session(self, client):
        client.post("/api/session", headers=_bearer())
        assert client.get("/api/channels").status_code == 200
        client.delete("/api/session")
        assert client.get("/api/channels").status_code == 401


class TestOperatorIdentity:
    def test_rest_post_cannot_spoof_author(self, client):
        r = client.post(
            "/api/channels/%23general/messages",
            headers=_bearer(),
            json={"author": "scout", "text": "hello"},
        )
        assert r.status_code == 200
        assert r.json()["author"] == "operator"

    def test_dm_cannot_spoof_sender(self, client):
        r = client.post(
            "/api/dm",
            headers=_bearer(),
            json={"sender": "scout", "recipient": "scout", "text": "hey"},
        )
        assert r.status_code == 200
        assert r.json()["channel"] == "#dm-operator-scout"
        assert r.json()["message"]["author"] == "operator"


class TestWebsocketEnforcement:
    def test_anonymous_websocket_is_rejected(self, client):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/%23general"):
                pass
        assert exc.value.code == 1008

    def test_websocket_accepts_valid_token_and_locks_identity(self, client):
        with client.websocket_connect(f"/ws/%23general?token={TOKEN}") as ws:
            history = ws.receive_json()
            assert history == {"type": "history", "messages": []}
            ws.send_text(
                '{"type": "post", "author": "scout", "text": "live hello"}')
            msg = ws.receive_json()
            assert msg["author"] == "operator"
            assert msg["text"] == "live hello"

    def test_websocket_accepts_session_cookie(self, client):
        client.post("/api/session", headers=_bearer())
        with client.websocket_connect("/ws/%23general") as ws:
            assert ws.receive_json()["type"] == "history"
