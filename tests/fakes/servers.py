"""Deterministic stand-ins for model servers and webhook receivers. Test use only."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Request, Response

DIM = 1024


def hashed_embedding(text: str) -> list[float]:
    """Bag of hashed words: documents sharing words with a query score higher, with no model needed."""
    vector = [0.0] * DIM
    for word in re.findall(r"\w+", text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        vector[int.from_bytes(digest[:4], "big") % DIM] += 1.0 if digest[4] % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    if not any(vector):
        vector[0], norm = 1.0, 1.0
    return [v / norm for v in vector]


def create_embedding_app(delay_s: float = 0.0, state: dict | None = None) -> FastAPI:
    state = state if state is not None else {}
    app = FastAPI()

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        if state.get("down"):
            return Response(status_code=503)
        body = await request.json()
        wait = state.get("delay_s", delay_s)
        if wait:
            await asyncio.sleep(wait)
        return {"data": [{"index": i, "embedding": hashed_embedding(t)} for i, t in enumerate(body["input"])],
                "model": body.get("model")}

    return app


def create_webhook_app(received: list, state: dict) -> FastAPI:
    app = FastAPI()

    @app.post("/hook")
    async def hook(request: Request):
        body = await request.body()
        received.append({"headers": dict(request.headers), "body": body, "json": await request.json()})
        return Response(status_code=state.get("status", 200))

    return app


def create_vision_app(state: dict) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/models")
    async def models():
        return {"data": [{"id": "fake"}]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        if state.get("down"):
            return Response(status_code=503)
        state["calls"] = state.get("calls", 0) + 1
        body = await request.json()
        parts = body["messages"][0]["content"]
        assert any(p["type"] == "image_url" and p["image_url"]["url"].startswith("data:image/") for p in parts)
        return {"choices": [{"message": {"content": state.get("reply", "# Vision Title\n\nvisionmarker page text")}}]}

    return app


class ServerThread:
    def __init__(self, app: FastAPI) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> ServerThread:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        return self

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
