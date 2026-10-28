"""Quarterdeck webchat server: KiwiIRC-style agent room.

Serves the web UI, a JSON REST API, and websocket live-tail per channel.
Run: quarterdeck serve [--port 8420]  (or python -m quarterdeck.web)
"""

from __future__ import annotations

import os
import re

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .agent_files import AGENT_FILES, AgentFiles
from .auth import AuthError, AuthGate, AuthSettings
from .agent_registry import AgentRegistry, RegistryError
from .chat import Chat, add_post_listener, sanitize_channel
from .models import DEFAULT_MODELS, resolve_model
from .thoughts import mount_thought_routes
from .users import UserError, UserRegistry
from .events import EventBus

STATIC_DIR = Path(__file__).parent / "static"


def available_models() -> list[str]:
    env = os.environ.get("QUARTERDECK_MODELS", "").strip()
    if env:
        return [m.strip() for m in env.split(",") if m.strip()]
    return list(DEFAULT_MODELS)


class PostMessage(BaseModel):
    author: str
    text: str
    cid: str | None = None


class CreateChannel(BaseModel):
    name: str


class CreateAgent(BaseModel):
    name: str
    system_prompt: str = ""
    model: str | None = None


class WriteFile(BaseModel):
    content: str


class DM(BaseModel):
    sender: str
    recipient: str
    text: str
    cid: str | None = None


class UpdateAgent(BaseModel):
    model: str | None = None


class SubToggle(BaseModel):
    channel: str
    subscribed: bool


class CreditBalance(BaseModel):
    balance_usd: float | None = None


class RegisterUser(BaseModel):
    username: str
    note: str = ""


class UpdateUserNote(BaseModel):
    note: str = ""


def _swallow_future(fut) -> None:
    try:
        fut.result()
    except Exception:
        pass


def dm_channel(a: str, b: str) -> str:
    """Deterministic DM channel name for two participants."""
    pair = sorted([a.strip(), b.strip()])
    return f"#dm-{pair[0]}-{pair[1]}"


def _author_for(conn, fallback: str) -> str:
    """Lock a signed-in browser to the operator identity when auth is on."""
    user = getattr(getattr(conn, "state", None), "user", None)
    if user:
        return str(user.get("name") or "operator")
    return fallback


def _session_payload(claims: dict) -> dict:
    return {
        "authenticated": True,
        "uid": claims.get("uid", ""),
        "email": claims.get("email", ""),
        "name": claims.get("name", "operator"),
        "auth_method": claims.get("auth_method", "firebase"),
    }


def _request_scheme(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower()
    return request.url.scheme


class Room:
    """Live room state: chat, registry, files, websocket subscribers."""

    def __init__(self, home: Path | None = None, daemon=None, *,
                 bus: EventBus | None = None, chat: Chat | None = None,
                 registry: AgentRegistry | None = None, runtime=None):
        self.runtime = runtime
        if runtime is not None:
            home = runtime.home
            daemon = runtime.daemon
            bus = runtime.bus
            chat = runtime.chat
            registry = runtime.registry
        self.bus = bus or EventBus()
        self.chat = chat or Chat(home=home, bus=self.bus)
        self.registry = registry or AgentRegistry(home=home)
        self.users = UserRegistry(home=self.chat.home)
        self.files = AgentFiles(home=self.chat.home)
        self.daemon = daemon
        self._subs: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Every stored message — including ones written by the agent daemon
        # in its own thread (thoughts, seed posts, agent DMs) — fans out to
        # websocket subscribers. This replaces per-endpoint broadcast calls.
        add_post_listener(self._on_chat_post)

    def _on_chat_post(self, channel: str, msg: dict) -> None:
        """Forward a stored message to WS subscribers. May run on any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self.broadcast(channel, msg), loop)
        except RuntimeError:
            return
        fut.add_done_callback(_swallow_future)

    def wake_agent(self, name: str, reason: str = "dm") -> bool:
        """Nudge an agent's runner to poll immediately (thread-safe)."""
        d = self.daemon
        if d is None:
            return False
        try:
            return bool(d.wake_agent(name, reason))
        except Exception:
            return False

    def wake_mentioned(self, text: str) -> None:
        """Wake any registered agent @mentioned in text."""
        for name in set(re.findall(r"@([A-Za-z0-9_-]+)", text or "")):
            self.wake_agent(name, "mention")

    def wake_subscribers(self, channel: str) -> None:
        """Wake every agent subscribed to this channel — any message wakes."""
        d = self.daemon
        if d is None:
            return
        from . import subscriptions
        try:
            runners = list(d.runners.keys())
        except Exception:
            return
        for name in runners:
            try:
                subs = subscriptions.get_subscriptions(name, home=self.chat.home)
                if channel in subs.get("channels", {}):
                    d.wake_agent(name, "channel-activity")
            except Exception:
                pass

    async def subscribe(self, channel: str, ws: WebSocket):
        channel = sanitize_channel(channel)
        async with self._lock:
            self._subs.setdefault(channel, set()).add(ws)

    async def unsubscribe(self, channel: str, ws: WebSocket):
        channel = sanitize_channel(channel)
        async with self._lock:
            self._subs.get(channel, set()).discard(ws)

    async def broadcast(self, channel: str, msg: dict):
        channel = sanitize_channel(channel)
        async with self._lock:
            targets = list(self._subs.get(channel, ()))
        import logging
        logging.getLogger("qd.ws").debug(
            "broadcast %s -> %d subs (msg %s)", channel, len(targets),
            str(msg.get("id", "?"))[:24])
        dead = []
        for ws in targets:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._subs.get(channel, set()).discard(ws)

    def post(self, channel: str, author: str, text: str,
             cid: str | None = None) -> dict:
        msg = self.chat.post(channel, author, text, cid=cid)
        return msg

    def handle_post(self, channel: str, author: str, text: str,
                    cid: str | None = None) -> dict:
        """Post and route wakeups exactly like a human message.

        REST, websocket, CLI, and internal posts all go through this path so a
        browser message can never bypass mention/subscription wakeups.
        """
        msg = self.post(channel, author, text, cid=cid)
        self.wake_mentioned(text)
        self.wake_subscribers(sanitize_channel(channel))
        return msg


def create_app(home: Path | None = None, daemon=None, *, runtime=None,
               bus: EventBus | None = None, chat: Chat | None = None,
               registry: AgentRegistry | None = None,
               auth: AuthSettings | None = None) -> FastAPI:
    room = Room(home=home, daemon=daemon, runtime=runtime, bus=bus, chat=chat,
                registry=registry)
    auth_settings = auth if auth is not None else AuthSettings.from_env()
    gate = AuthGate(
        None, auth_settings, login_page=STATIC_DIR / "login.html")
    app = FastAPI(title="Quarterdeck")
    app.state.auth_gate = gate

    @app.get("/health")
    def health():
        try:
            agents = len(room.registry.list())
        except Exception:
            agents = 0
        return {"ok": True, "version": __version__, "agents": agents}

    @app.get("/api/config")
    def public_config():
        """Public values the login screen and room need (safe to expose)."""
        settings = gate.settings
        return {
            "auth_required": gate.enabled,
            "firebase_project_id": settings.project_id,
            "firebase_auth_domain": settings.auth_domain,
            "firebase_api_key": settings.api_key,
            "site_url": settings.public_url,
        }

    @app.get("/api/session")
    def get_session(request: Request):
        if not gate.enabled:
            return {"authenticated": False, "auth_required": False}
        claims = gate.claims_from_scope(request.scope)
        if claims is None:
            raise HTTPException(401, "authentication required")
        return _session_payload(claims)

    @app.post("/api/session")
    def create_session(request: Request, response: Response):
        if not gate.enabled:
            return {"authenticated": True, "auth_required": False}
        try:
            token = gate._token_from_scope(request.scope, allow_query=False)
        except Exception:
            token = ""
        if not token:
            raise HTTPException(401, "missing bearer token")
        try:
            claims = gate.verify(token)
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        secure = _request_scheme(request) == "https"
        response.set_cookie(
            key=gate.settings.cookie_name,
            value=token,
            max_age=gate.settings.cookie_max_age,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
        )
        payload = _session_payload(claims)
        payload["auth_required"] = True
        return payload

    @app.delete("/api/session")
    def delete_session(response: Response):
        response.delete_cookie(gate.settings.cookie_name, path="/")
        return {"authenticated": False}

    @app.get("/api/channels")
    def list_channels():
        return {"channels": room.chat.channels()}

    @app.post("/api/channels")
    def create_channel(body: CreateChannel):
        name = sanitize_channel(body.name)
        # Creating = posting nothing; just validate + ensure it shows up.
        # We materialize by touching history (empty list is fine).
        room.chat.history(name, n=1)
        return {"channel": name}

    @app.get("/api/channels/{channel}/messages")
    def get_messages(channel: str, n: int = 50):
        return {"channel": sanitize_channel(channel),
                "messages": room.chat.history(channel, n=min(n, 200))}

    @app.post("/api/channels/{channel}/messages")
    async def post_message(channel: str, body: PostMessage, request: Request):
        author = _author_for(request, body.author)
        try:
            room.users.ensure(author)
        except UserError as e:
            raise HTTPException(400, str(e))
        try:
            msg = room.handle_post(channel, author, body.text, cid=body.cid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        # Broadcast happens via the chat post-listener; wakeups happen inside
        # handle_post so websocket and REST share one route.
        return msg

    @app.post("/api/dm")
    async def send_dm(body: DM, request: Request):
        sender = _author_for(request, body.sender)
        try:
            room.users.ensure(sender)
        except UserError as e:
            raise HTTPException(400, str(e))
        channel = dm_channel(sender, body.recipient)
        msg = room.post(channel, sender, f"(dm) {body.text}", cid=body.cid)
        # A DM always wakes the recipient agent immediately — no @mention needed.
        room.wake_agent(body.recipient, "dm")
        return {"channel": channel, "message": msg}

    @app.post("/api/users/register")
    def register_user(body: RegisterUser):
        """Explicit registration: pick a unique username."""
        try:
            profile = room.users.register(body.username, note=body.note)
        except UserError as e:
            raise HTTPException(409, str(e))
        return profile.to_dict()

    @app.get("/api/users")
    def list_users():
        return {"users": [u.to_dict() for u in room.users.list()]}

    @app.get("/api/users/{username}")
    def get_user(username: str):
        profile = room.users.get(username)
        if profile is None:
            raise HTTPException(404, "unknown user")
        return profile.to_dict()

    @app.put("/api/users/{username}/note")
    def update_user_note(username: str, body: UpdateUserNote):
        try:
            profile = room.users.update_note(username, body.note)
        except UserError as e:
            raise HTTPException(404, str(e))
        return profile.to_dict()

    @app.get("/api/agents")
    def list_agents():
        # Daemon heartbeat: agents the daemon actually has alive right now.
        alive: set[str] = set()
        try:
            import json as _json, time as _time
            hb_file = room.registry.home / "daemon.json"
            if hb_file.exists():
                hb = _json.loads(hb_file.read_text(encoding="utf-8"))
                now = _time.time()
                alive = {n for n, ts in hb.get("agents", {}).items()
                         if now - float(ts) < 90}
        except Exception:
            alive = set()
        agents = []
        # Thinking state from live runners (thread-safe attribute read).
        thinking = set()
        try:
            dmn = room.daemon
            if dmn is not None:
                for rn, runner in list(getattr(dmn, "runners", {}).items()):
                    if getattr(runner, "_thinking", False):
                        thinking.add(rn)
        except Exception:
            pass
        for a in room.registry.list():
            d = a.to_dict() if hasattr(a, "to_dict") else dict(a)
            name = d.get("name", "")
            if name in alive:
                d["presence"] = "working" if d.get("state") == "working" else "online"
            else:
                d["presence"] = "offline"
            d["thinking"] = name in thinking
            if name == "seed":
                d["seed"] = True
            agents.append(d)
        return {"agents": agents}

    @app.get("/api/models")
    def list_models():
        return {"models": available_models()}

    @app.patch("/api/agents/{name}")
    def update_agent(name: str, body: UpdateAgent):
        try:
            agent = room.registry.set_model(name, body.model)
        except RegistryError as e:
            raise HTTPException(404, str(e))
        # The runner picks up the model change on its next turn (no restart).
        room.wake_agent(name, "model-change")
        d = agent.to_dict() if hasattr(agent, "to_dict") else dict(agent)
        return d

    @app.get("/api/agents/{name}/subscriptions")
    def get_agent_subs(name: str):
        from . import subscriptions
        return subscriptions.get_subscriptions(name, home=room.chat.home)

    @app.post("/api/agents/{name}/subscriptions")
    def set_agent_sub(name: str, body: SubToggle):
        from . import subscriptions
        home = room.chat.home
        if body.subscribed:
            result = subscriptions.subscribe(name, [body.channel], home=home)
        else:
            result = subscriptions.unsubscribe(name, [body.channel], home=home)
        # Wake so the runner picks up subscription changes promptly.
        room.wake_agent(name, "sub-change")
        return result

    @app.get("/api/usage")
    def get_usage():
        from . import usage as usage_mod
        home = room.chat.home
        s = usage_mod.summary(home=home)
        return {
            "summary": s,
            "cache": {
                "hit_rate": s.get("cache_hit_rate"),
                "savings_usd": s.get("cache_savings_usd"),
                "savings_estimated": s.get("cache_savings_estimated"),
                "read_tokens": s.get("cache_read_tokens"),
                "write_tokens": s.get("cache_write_tokens"),
            },
            "burn_1h": usage_mod.burn_rate(home=home, window_hours=1.0),
            "burn_24h": usage_mod.burn_rate(home=home, window_hours=24.0),
            "eta": usage_mod.credit_eta(home=home),
        }

    @app.put("/api/usage/balance")
    def set_balance(body: CreditBalance):
        from . import usage as usage_mod
        return {"credit_balance_usd": usage_mod.set_credit_balance(
            body.balance_usd, home=room.chat.home)}

    @app.delete("/api/agents/{name}")
    async def delete_agent(name: str):
        d = room.daemon
        if d is None or getattr(d, "_loop", None) is None:
            raise HTTPException(500, "agent daemon not running")
        fut = asyncio.run_coroutine_threadsafe(d.deregister(name), d._loop)
        try:
            result = await asyncio.wrap_future(fut)
        except Exception as e:
            raise HTTPException(500, str(e))
        if result.get("status") == "error":
            raise HTTPException(400, result.get("error", "deregister failed"))
        if result.get("status") == "not-running":
            raise HTTPException(404, f"no running agent named {name!r}")
        return result

    @app.post("/api/agents/{name}/reset-env")
    async def reset_agent_env(name: str):
        """Wipe an agent's /work and rebuild their sandbox from the clean image."""
        d = room.daemon
        if d is None:
            raise HTTPException(500, "agent daemon not running")
        key = (name or "").strip()
        runner = d.runners.get(key)
        if runner is None:
            lowered = key.lower()
            for k, r in d.runners.items():
                if k.lower() == lowered:
                    runner = r
                    break
        if runner is None:
            raise HTTPException(404, f"no running agent named {name!r}")
        try:
            result = await asyncio.to_thread(runner.reset_environment)
        except Exception as e:
            raise HTTPException(500, f"reset failed: {e}")
        return {"agent": name, "result": result}

    @app.post("/api/agents")
    def create_agent(body: CreateAgent):
        try:
            agent = room.registry.spawn(
                body.name, system_prompt=body.system_prompt,
                model=resolve_model(body.model))
        except Exception as e:
            raise HTTPException(400, str(e))
        d = agent.to_dict() if hasattr(agent, "to_dict") else dict(agent)
        return d

    @app.get("/api/agents/{name}/files")
    def get_agent_files(name: str):
        return {"agent": name, "files": room.files.all(name)}

    @app.get("/api/agents/{name}/files/{filename}")
    def get_agent_file(name: str, filename: str):
        if filename not in AGENT_FILES:
            raise HTTPException(404, "unknown file")
        return {"agent": name, "file": filename,
                "content": room.files.read(name, filename)}

    @app.put("/api/agents/{name}/files/{filename}")
    def put_agent_file(name: str, filename: str, body: WriteFile):
        try:
            meta = room.files.write(name, filename, body.content)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return meta

    @app.websocket("/ws/{channel}")
    async def ws_channel(ws: WebSocket, channel: str):
        room._loop = asyncio.get_running_loop()
        await ws.accept()
        channel = sanitize_channel(channel)
        await room.subscribe(channel, ws)
        # Send recent history on connect so the client starts in sync.
        await ws.send_json({"type": "history",
                            "messages": room.chat.history(channel, n=50)})
        try:
            while True:
                data = await ws.receive_text()
                # Client pings / client-sent messages come through here.
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "post":
                    author = _author_for(ws, payload.get("author", "?"))
                    try:
                        room.handle_post(channel, author,
                                         payload.get("text", ""),
                                         cid=payload.get("cid"))
                    except ValueError:
                        continue
                elif payload.get("type") == "ping":
                    await ws.send_json({"type": "pong", "ts": time.time()})
        except WebSocketDisconnect:
            pass
        finally:
            await room.unsubscribe(channel, ws)

    # Install identity assets (served from the static directory at root paths).
    identity_assets = {
        "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
        "/manifest.json": ("manifest.webmanifest", "application/manifest+json"),
        "/site.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
        "/sw.js": ("sw.js", "application/javascript"),
        "/favicon.ico": ("favicon.ico", "image/x-icon"),
        "/favicon.svg": ("favicon.svg", "image/svg+xml"),
        "/favicon-16.png": ("favicon-16.png", "image/png"),
        "/favicon-32.png": ("favicon-32.png", "image/png"),
        "/favicon-48.png": ("favicon-48.png", "image/png"),
        "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
        "/icon-192.png": ("icon-192.png", "image/png"),
        "/icon-512.png": ("icon-512.png", "image/png"),
        "/icon-maskable-512.png": ("icon-maskable-512.png", "image/png"),
        "/og-image.png": ("og-image.png", "image/png"),
    }

    def _identity_endpoint(filename: str, media_type: str):
        def endpoint():
            path = STATIC_DIR / filename
            if not path.exists():
                raise HTTPException(status_code=404)
            if filename == "sw.js":
                headers = {"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"}
            else:
                headers = {"Cache-Control": "public, max-age=3600"}
            return FileResponse(str(path), media_type=media_type, headers=headers)
        return endpoint

    for route, (filename, media_type) in identity_assets.items():
        app.add_api_route(
            route, _identity_endpoint(filename, media_type),
            methods=["GET"], include_in_schema=False,
        )

    # Static UI (served last so /api routes win).
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def _index_response():
        idx = STATIC_DIR / "index.html"
        if not idx.exists():
            return JSONResponse({"ok": True, "hint": "static UI not built yet"})
        return FileResponse(str(idx))

    @app.get("/")
    def index():
        return _index_response()

    @app.get("/agents/room")
    def agents_room():
        """Compatibility alias for Eyry Pro's embedded room iframe."""
        return _index_response()

    # Thought streams (agent monologues)
    mount_thought_routes(app, room)
    # Firebase login gate: public login page + server-side API/ws enforcement.
    app.add_middleware(
        AuthGate, settings=auth_settings, login_page=STATIC_DIR / "login.html")
    # expose room for tests / embedding
    app.state.room = room
    return app


app = create_app()


def main() -> None:
    import argparse
    import uvicorn

    ap = argparse.ArgumentParser(description="Quarterdeck webchat server")
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
