"""Reference extraction server: wraps any in-process backend in the http backend's contract.

Run a backend in its own container, on any machine: `doclib extract-server`. A future extraction model can
implement the same three routes in whatever language it needs, and doclib only needs its URL.
"""

from __future__ import annotations

import hmac
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from ..config import ExtractionConfig
from ..log import event
from . import backends
from .backends.base import BackendError


def create_extract_app(config: ExtractionConfig, *, api_key_file: str | None = None) -> FastAPI:
    backend = backends.create_backend(config)
    api_key = Path(api_key_file).read_text(encoding="utf-8").strip() if api_key_file else None
    app = FastAPI(title="doclib extraction server")

    def check_key(authorization: str | None = Header(default=None)) -> None:
        if api_key and not hmac.compare_digest(authorization or "", f"Bearer {api_key}"):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/info", dependencies=[Depends(check_key)])
    async def info() -> dict[str, str]:
        return {"name": backend.name}

    @app.post("/v1/extract", dependencies=[Depends(check_key)])
    async def extract(request: Request, filename: str) -> dict:
        data = await request.body()
        try:
            result = await backend.extract(data, filename)
        except BackendError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        event("extract_server.extract", size_bytes=len(data), pages=len(result.pages))
        return asdict(result)

    @app.post("/v1/ocr", dependencies=[Depends(check_key)])
    async def ocr(request: Request, filename: str, pages: str | None = None) -> dict:
        data = await request.body()
        numbers = [int(p) for p in pages.split(",")] if pages else None
        try:
            recovered = await backend.ocr(data, filename, numbers)
        except BackendError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        event("extract_server.ocr", size_bytes=len(data), pages=len(recovered))
        return {"pages": {str(k): v for k, v in recovered.items()}}

    return app
