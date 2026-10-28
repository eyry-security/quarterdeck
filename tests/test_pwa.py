"""Installable PWA: manifest, icons, service worker, offline shell, install hint."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from quarterdeck.web import create_app

STATIC = Path(__file__).resolve().parent.parent / "quarterdeck" / "static"
MANIFEST = STATIC / "manifest.webmanifest"


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def sw_source() -> str:
    return (STATIC / "sw.js").read_text(encoding="utf-8")


def index_source() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(home=tmp_path))


class TestManifest:
    def test_required_schema(self):
        data = manifest()
        assert data["name"] == "Quarterdeck"
        assert data["short_name"] == "Quarterdeck"
        assert data["display"] == "standalone"
        assert data["orientation"] == "portrait"
        assert data["start_url"] == "/"
        assert data["scope"] == "/"
        assert data["id"] == "/"
        assert data["prefer_related_applications"] is False
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", data["background_color"])
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", data["theme_color"])
        assert isinstance(data["icons"], list) and data["icons"]

    def test_icons_match_manifest_and_files(self):
        found = set()
        for icon in manifest()["icons"]:
            path = STATIC / icon["src"].lstrip("/")
            assert path.exists(), icon["src"]
            assert icon["type"] == "image/png"
            width, height = (int(n) for n in icon["sizes"].split("x"))
            with Image.open(path) as image:
                assert image.size == (width, height)
            assert icon["purpose"] in {"any", "maskable"}
            found.add((icon["sizes"], icon["purpose"]))
        assert ("192x192", "any") in found
        assert ("512x512", "any") in found
        assert ("192x192", "maskable") in found
        assert ("512x512", "maskable") in found

    def test_apple_touch_icon_is_180(self):
        with Image.open(STATIC / "apple-touch-icon.png") as image:
            assert image.size == (180, 180)

    def test_served_with_manifest_content_type(self, client):
        response = client.get("/manifest.webmanifest")
        assert response.status_code == 200
        assert "manifest" in response.headers["content-type"]
        assert response.json()["name"] == "Quarterdeck"

    def test_icon_generator_is_reproducible(self):
        script = Path(__file__).resolve().parent.parent / "scripts" / "generate_icons.py"
        assert script.exists()
        source = script.read_text(encoding="utf-8")
        for name in (
            "icon-192.png",
            "icon-512.png",
            "icon-maskable-192.png",
            "icon-maskable-512.png",
            "apple-touch-icon.png",
        ):
            assert name in source


class TestDocument:
    def test_ios_metadata_and_viewport(self):
        html = index_source()
        assert 'name="viewport"' in html
        assert "viewport-fit=cover" in html
        assert 'name="apple-mobile-web-app-capable" content="yes"' in html
        assert 'name="apple-mobile-web-app-status-bar-style" content="black-translucent"' in html
        assert 'name="apple-mobile-web-app-title" content="Quarterdeck"' in html
        assert 'rel="apple-touch-icon"' in html
        assert 'rel="manifest" href="/manifest.webmanifest"' in html

    def test_safe_area_insets_in_shell(self):
        html = index_source()
        for edge in ("top", "bottom", "left", "right"):
            assert f"env(safe-area-inset-{edge})" in html


class TestServiceWorker:
    def test_served_from_root_with_scope(self, client):
        response = client.get("/sw.js")
        assert response.status_code == 200
        assert response.headers["service-worker-allowed"] == "/"
        assert "javascript" in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-cache"

    def test_versioned_cache_precaches_app_shell(self):
        source = sw_source()
        version = re.search(r'const VERSION = "([^"]+)"', source)
        assert version
        assert "quarterdeck-shell-" in source
        assert "const CACHE = CACHE_PREFIX + VERSION" in source
        shell = source.split("const SHELL = [", 1)[1].split("];", 1)[0]
        assert '"/static/index.html"' in shell
        assert 'const OFFLINE_URL = "/offline.html"' in source
        assert "OFFLINE_URL" in shell
        assert '"/manifest.webmanifest"' in shell
        assert '"/icon-192.png"' in shell
        assert '"/icon-maskable-192.png"' in shell
        assert "SHELL.includes(url.pathname)" in source

    def test_api_and_websocket_are_never_cached(self):
        source = sw_source()
        shell = source.split("const SHELL = [", 1)[1].split("];", 1)[0]
        assert "/api/" not in shell
        assert "/ws/" not in shell
        assert 'url.pathname.startsWith("/api/")' in source
        assert "event.respondWith(fetch(request))" in source
        assert 'url.pathname.startsWith("/ws/")' in source

    def test_update_flow_waits_for_user_refresh(self):
        source = sw_source()
        html = index_source()
        assert "SKIP_WAITING" in source
        assert "skipWaiting" in source
        assert "quarterdeck-shell-" in source
        assert 'register("/sw.js", {scope: "/"})' in html
        assert "controllerchange" in html
        assert "SKIP_WAITING" in html
        assert "Quarterdeck updated" in html

    def test_offline_screen_is_branded_and_retryable(self, client):
        response = client.get("/offline.html")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "You're offline" in response.text
        assert 'id="retry"' in response.text
        assert "Retry connection" in response.text


class TestInstallHint:
    def test_chromium_prompt_and_ios_sheet(self):
        html = index_source()
        assert "beforeinstallprompt" in html
        assert "appinstalled" in html
        assert "Add to Home Screen" in html
        assert "Share button" in html
        assert "qd-install-hint" in html
        assert "localStorage" in html
        assert "display-mode: standalone" in html

    def test_hint_is_one_time_and_dismissable(self):
        html = index_source()
        assert 'markInstallHint("shown")' in html
        assert 'markInstallHint("installed")' in html
        assert "installHintState()" in html
        assert 'installDismiss.addEventListener("click", hideInstallSheet)' in html
