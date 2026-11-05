"""Standalone vision model server: an OpenAI-compatible /v1/chat/completions for document pages.

The same server runs on CPU or GPU, locally or on a remote machine. doclib only needs its URL.
It accepts images inline only (never fetches URLs), keeps nothing on disk, and never logs document content.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import io
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import torch
from fastapi import Depends, FastAPI, Header, HTTPException
from PIL import Image
from pydantic import BaseModel
from transformers import AutoModelForImageTextToText, AutoProcessor

MODEL_NAME = os.environ.get("VISION_MODEL", "zai-org/GLM-OCR")
MODEL_PATH = os.environ.get("VISION_MODEL_PATH", "/models/GLM-OCR")
DEVICE = os.environ.get("VISION_DEVICE", "auto")
MAX_IMAGE_BYTES = int(os.environ.get("VISION_MAX_IMAGE_MB", "64")) * 1024 * 1024
MAX_NEW_TOKENS = int(os.environ.get("VISION_MAX_NEW_TOKENS", "8192"))
# Longest image side sent to the model. This bounds memory: a 170 DPI A4 page is about 2000 px.
MAX_LONG_SIDE = int(os.environ.get("VISION_MAX_LONG_SIDE", "1280"))
API_KEY_FILE = os.environ.get("VISION_API_KEY_FILE")
API_KEY = Path(API_KEY_FILE).read_text(encoding="utf-8").strip() if API_KEY_FILE else None


def log(name: str, **fields: Any) -> None:
    print(json.dumps({"ts": time.time(), "service": "doclib-vision", "event": name, **fields}), file=sys.stdout, flush=True)


def resolve_device() -> str:
    if DEVICE != "auto":
        return DEVICE
    return "cuda" if torch.cuda.is_available() else "cpu"


logging.getLogger("transformers").setLevel(logging.ERROR)
device = resolve_device()
# GPU: half precision. CPU: full precision by default, because bfloat16 is emulated on CPUs without AVX512/AMX or
# SVE (measured on arm64: no result in 280 s, against 127 s for float32). float32 needs about 5 GB of RAM.
dtype = {"auto": torch.bfloat16 if device == "cuda" else torch.float32, "bfloat16": torch.bfloat16,
         "float16": torch.float16, "float32": torch.float32}[os.environ.get("VISION_DTYPE", "auto")]
started = time.monotonic()
processor = AutoProcessor.from_pretrained(MODEL_PATH, local_files_only=True)
model = AutoModelForImageTextToText.from_pretrained(MODEL_PATH, local_files_only=True, dtype=dtype).to(device).eval()
log("model.loaded", model=MODEL_NAME, device=device, dtype=str(dtype), load_s=round(time.monotonic() - started, 1))

# A CPU or single GPU serves one page at a time; callers queue here.
inference_lock = threading.Lock()
app = FastAPI(title="doclib vision server")


def check_key(authorization: str | None = Header(default=None)) -> None:
    if API_KEY and not hmac.compare_digest(authorization or "", f"Bearer {API_KEY}"):
        raise HTTPException(status_code=401, detail="unauthorized")


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[dict[str, Any]]
    max_tokens: int | None = None
    temperature: float | None = 0


def parse_request(request: ChatRequest) -> tuple[Image.Image, str]:
    images: list[Image.Image] = []
    texts: list[str] = []
    for message in request.messages:
        content = message.get("content")
        for part in [{"type": "text", "text": content}] if isinstance(content, str) else content or []:
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if not url.startswith("data:") or ";base64," not in url:
                    raise HTTPException(status_code=400, detail="only inline base64 images are accepted")
                try:
                    raw = base64.b64decode(url.split(";base64,", 1)[1], validate=True)
                except binascii.Error as exc:
                    raise HTTPException(status_code=400, detail="invalid base64 image") from exc
                if len(raw) > MAX_IMAGE_BYTES:
                    raise HTTPException(status_code=413, detail="image too large")
                try:
                    images.append(Image.open(io.BytesIO(raw)).convert("RGB"))
                except OSError as exc:
                    raise HTTPException(status_code=400, detail="could not decode image") from exc
    if len(images) != 1:
        raise HTTPException(status_code=400, detail="exactly one image is required")
    return images[0], "\n".join(t for t in texts if t)


def generate(image: Image.Image, prompt: str, max_tokens: int) -> tuple[str, int]:
    if max(image.size) > MAX_LONG_SIDE:
        image.thumbnail((MAX_LONG_SIDE, MAX_LONG_SIDE), Image.Resampling.LANCZOS)
    messages = [{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": prompt}]}]
    inputs = processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=True, return_dict=True,
                                           return_tensors="pt")
    inputs = inputs.to(device)
    inputs.pop("token_type_ids", None)
    with inference_lock, torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
    new_tokens = output[0][inputs["input_ids"].shape[1]:]
    return processor.decode(new_tokens, skip_special_tokens=True), int(new_tokens.shape[0])


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "device": device, "model": MODEL_NAME}


@app.get("/v1/models", dependencies=[Depends(check_key)])
def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "doclib"}]}


@app.post("/v1/chat/completions", dependencies=[Depends(check_key)])
async def chat(request: ChatRequest) -> dict[str, Any]:
    image, prompt = parse_request(request)
    limit = min(request.max_tokens or MAX_NEW_TOKENS, MAX_NEW_TOKENS)
    began = time.monotonic()
    text, tokens = await asyncio.to_thread(generate, image, prompt, limit)
    duration = time.monotonic() - began
    log("page.completed", device=device, width=image.width, height=image.height, tokens=tokens, duration_s=round(duration, 2))
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion", "created": int(time.time()), "model": MODEL_NAME,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {"completion_tokens": tokens},
    }
