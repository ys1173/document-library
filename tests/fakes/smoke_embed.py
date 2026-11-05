"""ASGI apps for the end-to-end smoke test."""

from __future__ import annotations

from fastapi import FastAPI, Request

from tests.fakes.servers import create_embedding_app

app = create_embedding_app()
hook_app = FastAPI()
messages: list[dict] = []


@hook_app.post("/hook")
async def hook(request: Request) -> dict:
    messages.append(await request.json())
    return {"ok": True}


@hook_app.get("/messages")
async def get_messages() -> list[dict]:
    return messages
