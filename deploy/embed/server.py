"""Standalone embedding server: an OpenAI-compatible /v1/embeddings for testing and small sites.

Production can use any existing OpenAI-compatible embedding service instead; doclib only needs its URL.
The same server runs on CPU or GPU. It never fetches anything, keeps nothing on disk, and never logs input text.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import torch
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from transformers import AutoModel, AutoTokenizer

MODEL_NAME = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
MODEL_PATH = os.environ.get("EMBED_MODEL_PATH", "/models/bge-m3")
DEVICE = os.environ.get("EMBED_DEVICE", "auto")
MAX_LENGTH = int(os.environ.get("EMBED_MAX_LENGTH", "4096"))  # Tokens per input. BGE-M3 supports up to 8192.
BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "16"))
MAX_INPUTS = int(os.environ.get("EMBED_MAX_INPUTS", "256"))
API_KEY_FILE = os.environ.get("EMBED_API_KEY_FILE")
API_KEY = Path(API_KEY_FILE).read_text(encoding="utf-8").strip() if API_KEY_FILE else None


def log(name: str, **fields: Any) -> None:
    print(json.dumps({"ts": time.time(), "service": "doclib-embed", "event": name, **fields}), file=sys.stdout, flush=True)


def resolve_device() -> str:
    if DEVICE != "auto":
        return DEVICE
    return "cuda" if torch.cuda.is_available() else "cpu"


logging.getLogger("transformers").setLevel(logging.ERROR)
device = resolve_device()
dtype = torch.float16 if device == "cuda" else torch.float32
started = time.monotonic()
tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True)
model = AutoModel.from_pretrained(MODEL_PATH, local_files_only=True, dtype=dtype).to(device).eval()
log("model.loaded", model=MODEL_NAME, device=device, dtype=str(dtype), load_s=round(time.monotonic() - started, 1))

lock = threading.Lock()
app = FastAPI(title="doclib embedding server")


def check_key(authorization: str | None = Header(default=None)) -> None:
    if API_KEY and not hmac.compare_digest(authorization or "", f"Bearer {API_KEY}"):
        raise HTTPException(status_code=401, detail="unauthorized")


class EmbeddingRequest(BaseModel):
    model: str | None = None
    input: str | list[str]


def embed(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = tokenizer(texts[start:start + BATCH_SIZE], padding=True, truncation=True, max_length=MAX_LENGTH,
                          return_tensors="pt").to(device)
        with lock, torch.inference_mode():
            output = model(**batch)
        # BGE models use the CLS token, L2-normalised, as the dense embedding.
        dense = torch.nn.functional.normalize(output.last_hidden_state[:, 0], p=2, dim=1)
        vectors.extend(dense.float().cpu().tolist())
    return vectors


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "device": device, "model": MODEL_NAME}


@app.get("/v1/models", dependencies=[Depends(check_key)])
def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "doclib"}]}


@app.post("/v1/embeddings", dependencies=[Depends(check_key)])
async def embeddings(request: EmbeddingRequest) -> dict[str, Any]:
    texts = [request.input] if isinstance(request.input, str) else request.input
    if not texts or len(texts) > MAX_INPUTS:
        raise HTTPException(status_code=400, detail=f"send 1 to {MAX_INPUTS} inputs")
    began = time.monotonic()
    vectors = await asyncio.to_thread(embed, texts)
    log("batch.completed", device=device, inputs=len(texts), duration_s=round(time.monotonic() - began, 3))
    return {"object": "list", "model": MODEL_NAME, "usage": {"prompt_tokens": 0, "total_tokens": 0},
            "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]}
