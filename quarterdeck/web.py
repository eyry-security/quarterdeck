"""Quarterdeck webchat server: KiwiIRC-style agent room.

Serves the web UI, a JSON REST API, and websocket live-tail per channel.
Run: quarterdeck serve [--port 8420]  (or python -m quarterdeck.web)
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent_files import AGENT_FILES, AgentFiles
from .agent_registry import AgentRegistry
from .chat import Chat, sanitize_channel
from .events import EventBus

STATIC_DIR = Path(__file__).parent / "static"


class PostMessage(BaseModel):
    author: str
    text: str


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


def dm_channel(a: str, b: str) -> str:
    """Deterministic DM channel name for two participants."""
    pair = sorted([a.strip(), b.strip()])
    return f"#dm-{pair[0]}-{pair[1]}"


class Room:
    """Live room state: chat, registry, files, websocket subscribers."""

    def __init__(self, home: Path | None = None):
        self.bus = EventBus()
        self.chat = Chat(home=home, bus=self.bus)
        self.registry = AgentRegistry(home=home)
        self.files = AgentFiles(home=home)
        self._subs: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

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

    def post(self, channel: str, author: str, text: str) -> dict:
        msg = self.chat.post(channel, author, text)
        return msg


def create_app(home: Path | None = None) -> FastAPI:
    room = Room(home=home)
    app = FastAPI(title="Quarterdeck")

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
    async def post_message(channel: str, body: PostMessage):
        try:
            msg = room.post(channel, body.author, body.text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        await room.broadcast(channel, msg)
        return msg

    @app.post("/api/dm")
    async def send_dm(body: DM):
        channel = dm_channel(body.sender, body.recipient)
        msg = room.post(channel, body.sender, f"(dm) {body.text}")
        await room.broadcast(channel, msg)
        return {"channel": channel, "message": msg}

    @app.get("/api/agents")
    def list_agents():
        agents = []
        for a in room.registry.list():
            d = a.to_dict() if hasattr(a, "to_dict") else dict(a)
            d["presence"] = "working" if d.get("state") == "working" else "online"
            agents.append(d)
        return {"agents": agents}

    @app.post("/api/agents")
    def create_agent(body: CreateAgent):
        try:
            agent = room.registry.spawn(
                body.name, system_prompt=body.system_prompt, model=body.model)
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
                    try:
                        msg = room.post(channel, payload.get("author", "?"),
                                        payload.get("text", ""))
                    except ValueError:
                        continue
                    await room.broadcast(channel, msg)
                elif payload.get("type") == "ping":
                    await ws.send_json({"type": "pong", "ts": time.time()})
        except WebSocketDisconnect:
            pass
        finally:
            await room.unsubscribe(channel, ws)

    # Static UI (served last so /api routes win).
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        idx = STATIC_DIR / "index.html"
        if not idx.exists():
            return JSONResponse({"ok": True, "hint": "static UI not built yet"})
        return FileResponse(str(idx))

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
