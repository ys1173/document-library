"""Change notifications through one standard outbound webhook (spec section 5.2).

Two steps, so a crash can neither lose a message nor send a different one:
  1. compile: in one transaction, group finished change events into digests stored in an outbox.
  2. deliver: POST outbox rows with retries. Delivery is at-least-once, so receivers de-duplicate on event_id.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx

from . import telemetry
from .config import Settings, WebhookConfig
from .log import event, safe_error

SCHEMA_VERSION = "1.0"
EVENT_TYPE = "doclib.source.changes"
POLL_S = 5.0


def summarise(changes: list[dict[str, Any]]) -> dict[str, int]:
    kinds = Counter(c["change"] for c in changes)
    outcomes = Counter(c["outcome"] for c in changes)
    return {"new": kinds["new"], "changed": kinds["changed"], "deleted": kinds["deleted"], "moved": kinds["moved"],
            "indexed": outcomes["indexed"], "failed": outcomes["failed"], "skipped": outcomes["skipped"]}


def summary_text(source_id: str, summary: dict[str, int]) -> str:
    parts = [f"{summary[k]} {k}" for k in ("new", "changed", "deleted", "moved") if summary[k]]
    parts += [f"{summary[k]} {k}" for k in ("failed", "skipped") if summary[k]]
    return f"{source_id}: {', '.join(parts) or 'no changes'}"


def build_payloads(*, tenant_id: str, source_id: str, scan_id: str, changes: list[dict[str, Any]],
                   max_changes: int, now: datetime | None = None) -> list[tuple[str, dict[str, Any]]]:
    """Digest messages for one scan, split into parts when large. The summary always covers this part."""
    now = now or datetime.now(UTC)
    size = max(1, max_changes)
    groups = [changes[i:i + size] for i in range(0, len(changes), size)] or [[]]
    messages: list[tuple[str, dict[str, Any]]] = []
    for number, group in enumerate(groups, start=1):
        delivery_id = str(uuid.uuid4())
        summary = summarise(group)
        messages.append((delivery_id, {
            "schema_version": SCHEMA_VERSION,
            "event_id": delivery_id,
            "event_type": EVENT_TYPE,
            "occurred_at": now.isoformat().replace("+00:00", "Z"),
            "tenant_id": tenant_id,
            "source_id": source_id,
            "scan_id": scan_id,
            "part": number,
            "parts": len(groups),
            "text": summary_text(source_id, summary),
            "summary": summary,
            "changes": group,
        }))
    return messages


async def compile_digests(pool: asyncpg.Pool, config: WebhookConfig) -> int:
    """Group ready events. A scan goes out once it has settled, or after digest_max_wait for what is ready."""
    groups = await pool.fetch(
        """
        select tenant_id, source_id, scan_id::text from doclib.change_events
         where delivery_id is null
         group by tenant_id, source_id, scan_id
        having count(*) filter (where state = 'ready') > 0
           and (count(*) filter (where state = 'pending') = 0
                or min(created_at) filter (where state = 'ready') < now() - make_interval(secs => $1))
        """, float(config.digest_max_wait))
    created = 0
    for group in groups:
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "select event_id, file_id::text, change, path, previous_path, content_version, outcome, error "
                "from doclib.change_events where scan_id = $1::uuid and delivery_id is null and state = 'ready' "
                "order by event_id for update skip locked", group["scan_id"])
            if not rows:
                continue
            changes = [{"change": r["change"], "outcome": r["outcome"], "file_id": r["file_id"], "path": r["path"],
                        "previous_path": r["previous_path"], "content_version": r["content_version"],
                        "error": r["error"]} for r in rows]
            messages = build_payloads(tenant_id=group["tenant_id"], source_id=group["source_id"],
                                      scan_id=group["scan_id"], changes=changes,
                                      max_changes=config.max_changes_per_message)
            size = max(1, config.max_changes_per_message)
            for position, (delivery_id, payload) in enumerate(messages):
                await conn.execute(
                    "insert into doclib.webhook_deliveries (delivery_id, tenant_id, source_id, payload) "
                    "values ($1, $2, $3, $4)", delivery_id, group["tenant_id"], group["source_id"], payload)
                ids = [r["event_id"] for r in rows[position * size:(position + 1) * size]]
                await conn.execute("update doclib.change_events set delivery_id = $1 where event_id = any($2::bigint[])",
                                   delivery_id, ids)
                created += 1
    if created:
        event("webhook.digests_compiled", messages=created)
    return created


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


async def deliver_due(pool: asyncpg.Pool, config: WebhookConfig, client: httpx.AsyncClient, url: str,
                      secret: str | None) -> int:
    rows = await pool.fetch(
        "select delivery_id::text, payload, attempts, source_id from doclib.webhook_deliveries "
        "where status = 'pending' and next_attempt_at <= now() order by created_at limit 20")
    sent = 0
    for row in rows:
        body = json.dumps(row["payload"], separators=(",", ":"), ensure_ascii=False).encode()
        timestamp = str(int(time.time()))
        headers = {"Content-Type": "application/json", "X-Doclib-Event-Id": row["delivery_id"],
                   "X-Doclib-Timestamp": timestamp, "User-Agent": "doclib-webhook/1"}
        if secret:
            headers["X-Doclib-Signature"] = sign(secret, timestamp, body)
        attempts = row["attempts"] + 1
        error: str | None = None
        try:
            response = await client.post(url, content=body, headers=headers, timeout=config.timeout_s)
            if response.status_code >= 300:
                error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            error = type(exc).__name__  # Never log the URL: it may carry a token.
        if error is None:
            await pool.execute(
                "update doclib.webhook_deliveries set status = 'sent', attempts = $2, sent_at = now(), last_error = null "
                "where delivery_id = $1", row["delivery_id"], attempts)
            telemetry.webhook_sent.add(1, {"result": "sent"})
            event("webhook.sent", delivery_id=row["delivery_id"], source_id=row["source_id"], attempts=attempts,
                  summary=row["payload"].get("summary"))
            sent += 1
        elif attempts >= config.max_attempts:
            await pool.execute(
                "update doclib.webhook_deliveries set status = 'dead', attempts = $2, last_error = $3 where delivery_id = $1",
                row["delivery_id"], attempts, error)
            telemetry.webhook_sent.add(1, {"result": "dead"})
            event("webhook.dead", level=40, delivery_id=row["delivery_id"], source_id=row["source_id"], error=error)
        else:
            delay = min(config.max_backoff_s, 5 * (2 ** (attempts - 1)))
            await pool.execute(
                "update doclib.webhook_deliveries set attempts = $2, last_error = $3, "
                "next_attempt_at = now() + make_interval(secs => $4) where delivery_id = $1",
                row["delivery_id"], attempts, error, float(delay))
            telemetry.webhook_sent.add(1, {"result": "retry"})
            event("webhook.retry", level=30, delivery_id=row["delivery_id"], error=error, attempts=attempts, delay_s=delay)
    return sent


@dataclass(frozen=True)
class WebhookTarget:
    enabled: bool
    url: str | None
    secret: str | None
    secret_env: str | None  # Name of the variable holding the signing secret, if set from the portal.
    origin: str  # portal or config
    problem: str | None = None  # Set when it can't safely send.


async def load_target(pool: asyncpg.Pool, config: WebhookConfig) -> WebhookTarget:
    """Settings saved from the portal win over the config file. Read on every poll, so a change needs no restart."""
    saved = await pool.fetchval("select value from doclib.settings where key = 'webhook'")
    if saved is None:
        return WebhookTarget(config.enabled, config.resolve_url() if config.enabled else None,
                             config.resolve_secret() if config.enabled else None, None, "config")
    secret_env = saved.get("secret_env")
    secret = os.environ.get(secret_env) if secret_env else config.resolve_secret()
    # Never fall back to sending unsigned when signing was asked for.
    problem = f"{secret_env} is not set on this service" if secret_env and not secret else None
    return WebhookTarget(saved["enabled"], saved.get("url"), secret, secret_env, "portal", problem)


async def poll_once(pool: asyncpg.Pool, config: WebhookConfig, client: httpx.AsyncClient, target: WebhookTarget) -> None:
    if target.enabled and target.url and not target.problem:
        await compile_digests(pool, config)
        await deliver_due(pool, config, client, target.url, target.secret)


async def run_forever(pool: asyncpg.Pool, settings: Settings) -> None:
    config = settings.notifications.webhook
    state: tuple[bool, bool, str | None] | None = None
    async with httpx.AsyncClient() as client:
        while True:
            try:
                target = await load_target(pool, config)
                current = (bool(target.enabled and target.url), bool(target.secret), target.problem)
                if current != state:
                    if target.problem:
                        event("webhook.misconfigured", level=40, error=target.problem)
                    elif current[0]:
                        event("webhook.enabled", signed=current[1], origin=target.origin)
                    else:
                        event("webhook.disabled", origin=target.origin)
                    state = current
                await poll_once(pool, config, client, target)
            except (OSError, asyncpg.PostgresError) as exc:
                event("webhook.loop_error", level=40, error=safe_error(exc))
            await asyncio.sleep(POLL_S)
