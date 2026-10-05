"""Thought stream support for Quarterdeck web API."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel


class ThoughtIn(BaseModel):
    text: str


def thought_channel(agent: str) -> str:
    """Canonical thought-stream channel for an agent."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in agent.strip().lower())
    if not safe:
        raise ValueError("agent name must not be empty")
    return f"#thoughts-{safe}"


def mount_thought_routes(app, room):
    """Add thought-stream API + websocket routes to the FastAPI app."""

    @app.post("/api/agents/{name}/thoughts")
    async def post_thought(name: str, body: ThoughtIn):
        """Append a thought to an agent's thought stream."""
        if not body.text or not body.text.strip():
            raise HTTPException(400, "thought text must not be empty")
        if len(body.text) > 20_000:
            raise HTTPException(400, "thought text too long")
        channel = thought_channel(name)
        try:
            msg = room.post(channel, name, body.text.strip())
        except ValueError as e:
            raise HTTPException(400, str(e))
        # Tag as a thought for distinct frontend rendering.
        msg["thought"] = True
        await room.broadcast(channel, msg)
        return msg

    @app.get("/api/agents/{name}/thoughts")
    async def get_thoughts(name: str, limit: int = 50):
        """Recent thoughts for an agent, newest last."""
        channel = thought_channel(name)
        msgs = room.chat.history(channel, n=max(1, min(limit, 200)))
        for m in msgs:
            m["thought"] = True
        return {"agent": name, "thoughts": msgs}

    @app.websocket("/ws/thoughts/{name}")
    async def ws_thoughts(ws: WebSocket, name: str):
        """Live thought stream for an agent."""
        await ws.accept()
        try:
            channel = thought_channel(name)
        except ValueError:
            await ws.close(code=4000)
            return
        await room.subscribe(channel, ws)
        await ws.send_json({
            "type": "history",
            "messages": [{**m, "thought": True}
                         for m in room.chat.history(channel, n=50)],
        })
        try:
            while True:
                data = await ws.receive_text()
                # Thoughts are append-only from the client side; ignore
                # everything except pings to keep the stream clean.
                if data.strip() == "ping":
                    await ws.send_json({"type": "pong", "ts": time.time()})
        except WebSocketDisconnect:
            pass
        finally:
            await room.unsubscribe(channel, ws)
