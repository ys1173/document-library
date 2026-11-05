"""Client for any OpenAI-compatible /v1/embeddings endpoint (spec section 8)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from . import telemetry
from .config import EmbeddingsConfig


class EmbeddingError(Exception):
    """The service is down or misconfigured. Not the file's fault, so jobs wait without using up attempts."""


class EmbeddingRejected(Exception):
    """The service refused this input (for example, too long for the model). Retrying won't help, so attempts count."""


class EmbeddingClient:
    def __init__(self, config: EmbeddingsConfig) -> None:
        self.config = config
        headers = {}
        if config.api_key_file:
            headers["Authorization"] = f"Bearer {Path(config.api_key_file).read_text(encoding='utf-8').strip()}"
        self._client = httpx.AsyncClient(base_url=config.endpoint.rstrip("/"), timeout=config.timeout_s, headers=headers)
        self._limit = asyncio.Semaphore(max(1, config.concurrency))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        delay = 1.0
        last: Exception | None = None
        for _ in range(3):
            try:
                async with self._limit:
                    response = await self._client.post("/embeddings", json={"model": self.config.model, "input": texts})
                if response.status_code in {400, 413, 422}:
                    raise EmbeddingRejected(f"HTTP {response.status_code}: the service refused this input")
                response.raise_for_status()
                data = sorted(response.json()["data"], key=lambda d: d["index"])
                vectors = [d["embedding"] for d in data]
                if len(vectors) != len(texts):
                    raise EmbeddingError(f"expected {len(texts)} vectors, got {len(vectors)}")
                for vector in vectors:
                    if len(vector) != self.config.dimension:
                        raise EmbeddingError(
                            f"model returned dimension {len(vector)}, configured {self.config.dimension}")
                return vectors
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last = exc
                await asyncio.sleep(delay)
                delay *= 2
        status = f" (HTTP {last.response.status_code})" if isinstance(last, httpx.HTTPStatusError) else ""
        raise EmbeddingError(f"embedding request failed: {type(last).__name__}{status}") from last

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.embed([f"{self.config.document_prefix}{t}" for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed([f"{self.config.query_prefix}{text}"]))[0]

    async def check(self) -> dict[str, object]:
        """Is the service reachable and returning what's configured? For `doclib check`."""
        vector = (await self.embed_query("connectivity check"))
        return {"ok": True, "model": self.config.model, "dimension": len(vector)}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        size = max(1, self.config.batch_size)
        batches = [texts[i:i + size] for i in range(0, len(texts), size)]
        results = await asyncio.gather(*(self._embed_batch(b) for b in batches))
        telemetry.embedded_chunks.add(len(texts))
        return [vector for batch in results for vector in batch]


def to_pgvector(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.7g}" for v in vector) + "]"
