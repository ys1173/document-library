"""A small web portal for operators, served on the admin port: status, sources, webhook and models.

It's meant for an internal network, so a login is optional: set DOCLIB_PORTAL_PASSWORD to turn it on. It has its own
routes because the REST API may require bearer tokens, which a browser doesn't have. Models are shown but not changed.
"""

import asyncio
import hmac
import json
import os
import re
import time
import uuid
from base64 import b64decode
from datetime import UTC, datetime
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from . import notify
from .config import Settings
from .connectors import ConnectorError, ConnectorSpec, backend_detail, backend_summary
from .embeddings import EmbeddingError, EmbeddingRejected
from .log import event
from .vlm import vlm_reachable

CALLER = "portal"
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PAGE_HEADERS = {"Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"}


class NewConnector(ConnectorSpec):
    id: str


class WebhookForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    url: str | None = None
    secret_env: str | None = None


class SearchForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str


def login_matches(header: str | None, username: str, password: str) -> bool:
    scheme, _, encoded = (header or "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        given = b64decode(encoded, validate=True)
    except ValueError:
        return False
    return hmac.compare_digest(given, f"{username}:{password}".encode())


def check_webhook_form(form: WebhookForm, prefix: str) -> tuple[str | None, str | None]:
    """The cleaned URL and secret variable name. Raises if they can't be used."""
    url = (form.url or "").strip() or None
    secret_env = (form.secret_env or "").strip() or None
    problems: list[str] = []
    if url:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            problems.append("URL must start with http:// or https://")
    elif form.enabled:
        problems.append("URL is required")
    if secret_env:
        if not _ENV_NAME.match(secret_env) or not secret_env.startswith(prefix):
            problems.append(f"Secret variable must start with {prefix}")
        elif secret_env not in os.environ:
            problems.append(f"{secret_env} is not set on this service")
    if problems:
        raise ConnectorError("invalid_webhook", "the webhook settings are not valid", details=problems)
    return url, secret_env


async def send_test(url: str, secret: str | None, tenant_id: str, timeout: float) -> dict[str, Any]:
    event_id = str(uuid.uuid4())
    body = json.dumps({"schema_version": notify.SCHEMA_VERSION, "event_id": event_id, "event_type": "doclib.test",
                       "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "tenant_id": tenant_id,
                       "text": "Test from Document Library"}, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Doclib-Event-Id": event_id, "X-Doclib-Timestamp": timestamp,
               "User-Agent": "doclib-webhook/1"}
    if secret:
        headers["X-Doclib-Signature"] = notify.sign(secret, timestamp, body)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, content=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": type(exc).__name__}  # Never the message: it can quote the URL.
    return {"ok": response.status_code < 300, "status": response.status_code}


def create_portal_router(settings: Settings, state: dict[str, Any]) -> APIRouter:
    username = os.environ.get("DOCLIB_PORTAL_USERNAME") or "admin"
    password = os.environ.get("DOCLIB_PORTAL_PASSWORD") or None
    tenant = settings.tenant_id
    webhook_config = settings.notifications.webhook
    prefix = settings.connectors.credential_env_prefix
    if not password:
        event("admin.portal_no_login", level=30, error="DOCLIB_PORTAL_PASSWORD is not set, so the portal has no login")

    def guard(request: Request) -> None:
        if password and not login_matches(request.headers.get("authorization"), username, password):
            event("admin.portal_login_rejected", level=30, path=request.url.path)
            raise HTTPException(401, "login required", headers={"WWW-Authenticate": 'Basic realm="Document Library", charset="UTF-8"'})
        # Browsers won't send a custom header across sites without permission, so other sites can't trigger actions.
        if request.method not in {"GET", "HEAD"} and request.headers.get("x-portal") != "1":
            raise ConnectorError("forbidden", "missing X-Portal header", status=403)

    router = APIRouter(dependencies=[Depends(guard)], include_in_schema=False)

    def pool() -> Any:
        return state["service"].pool

    @router.get("/")
    async def page() -> HTMLResponse:
        html = (resources.files("doclib") / "portal.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers=_PAGE_HEADERS)

    @router.get("/portal/api/status")
    async def status() -> dict[str, Any]:
        library = state["library"]
        return jsonable_encoder({**await library.index_status(tenant), **await library.list_sources(tenant)})

    @router.post("/portal/api/search")
    async def search(form: SearchForm) -> dict[str, Any]:
        return jsonable_encoder(await state["library"].search_docs(tenant, form.query, 5))

    @router.get("/portal/api/connector-types")
    async def connector_types() -> dict[str, Any]:
        return {"types": [backend_summary(e) for _, e in sorted((await state["service"].catalogue()).items())]}

    @router.get("/portal/api/connector-types/{type_name}")
    async def connector_type(type_name: str) -> dict[str, Any]:
        entry = (await state["service"].catalogue()).get(type_name)
        if entry is None:
            raise ConnectorError("unsupported_type", f"type {type_name!r} is not available", status=404)
        detail = backend_detail(entry)
        options = [o for o in detail["options"] if not o["advanced"]]
        return {**detail, "options": sorted(options, key=lambda o: not o["required"])}

    @router.get("/portal/api/connectors")
    async def connectors() -> dict[str, Any]:
        return jsonable_encoder({"connectors": await state["service"].list(tenant)})

    @router.post("/portal/api/connectors/test")
    async def test_connector(body: NewConnector) -> dict[str, Any]:
        spec = ConnectorSpec.model_validate(body.model_dump(exclude={"id"}))
        source = await state["service"].validate(body.id, spec, tenant)
        return jsonable_encoder(await state["service"].test(source))

    @router.post("/portal/api/connectors", status_code=201)
    async def create_connector(body: NewConnector) -> dict[str, Any]:
        spec = ConnectorSpec.model_validate(body.model_dump(exclude={"id"}))
        connector, _ = await state["service"].apply(body.id, spec, tenant, create_only=True, test=True, caller=CALLER)
        return jsonable_encoder(connector)

    @router.post("/portal/api/connectors/{connector_id}/scan", status_code=202)
    async def scan(connector_id: str) -> dict[str, bool]:
        await state["service"].request_scan(connector_id, tenant, caller=CALLER)
        return {"scan_requested": True}

    @router.delete("/portal/api/connectors/{connector_id}")
    async def delete(connector_id: str) -> dict[str, int]:
        return await state["service"].delete(connector_id, tenant, caller=CALLER)

    @router.get("/portal/api/webhook")
    async def webhook() -> dict[str, Any]:
        target = await notify.load_target(pool(), webhook_config)
        rows = await pool().fetch(
            "select source_id, payload->>'text' as text, status, attempts, last_error, created_at, sent_at "
            "from doclib.webhook_deliveries where tenant_id = $1 order by created_at desc limit 20", tenant)
        return jsonable_encoder({"enabled": target.enabled, "url": target.url, "secret_env": target.secret_env,
                                 "signed": bool(target.secret), "origin": target.origin, "problem": target.problem,
                                 "deliveries": [dict(r) for r in rows]})

    @router.put("/portal/api/webhook")
    async def save_webhook(form: WebhookForm) -> dict[str, Any]:
        url, secret_env = check_webhook_form(form, prefix)
        before = await notify.load_target(pool(), webhook_config)
        async with pool().acquire() as conn, conn.transaction():
            await conn.execute(
                "insert into doclib.settings (key, value) values ('webhook', $1) "
                "on conflict (key) do update set value = excluded.value, updated_at = now()",
                {"enabled": form.enabled, "url": url, "secret_env": secret_env})
            if form.enabled and not before.enabled:
                # Changes are recorded while the webhook is off. Turning it on must not send all of them at once.
                await conn.execute("delete from doclib.change_events where delivery_id is null and state = 'ready'")
            if url != before.url:
                # Messages waiting out a backoff for the old address go to the new one straight away.
                await conn.execute("update doclib.webhook_deliveries set next_attempt_at = now() where status = 'pending'")
        event("webhook.settings_changed", enabled=form.enabled, signed=bool(secret_env), caller=CALLER)  # Never the URL.
        return await webhook()

    @router.post("/portal/api/webhook/test")
    async def test_webhook(form: WebhookForm) -> dict[str, Any]:
        url, secret_env = check_webhook_form(form.model_copy(update={"enabled": True}), prefix)
        secret = os.environ[secret_env] if secret_env else webhook_config.resolve_secret()
        result = await send_test(url, secret, tenant, webhook_config.timeout_s)
        event("webhook.test_sent", ok=result["ok"], status=result.get("status"), caller=CALLER)
        return result

    @router.post("/portal/api/webhook/retry")
    async def retry_failed() -> dict[str, int]:
        result = await pool().execute(
            "update doclib.webhook_deliveries set status = 'pending', attempts = 0, next_attempt_at = now() "
            "where status = 'dead' and tenant_id = $1", tenant)
        count = int(result.rsplit(" ", 1)[1])
        event("webhook.retry_requested", deliveries=count, caller=CALLER)
        return {"retried": count}

    @router.get("/portal/api/models")
    async def models() -> dict[str, Any]:
        async def embedding_ok() -> bool:
            try:
                await asyncio.wait_for(state["embedder"].check(), 15)
                return True
            except (TimeoutError, EmbeddingError, EmbeddingRejected, OSError):
                return False

        text, vlm, embeddings = settings.extraction.text, settings.extraction.vlm, settings.embeddings
        chunks = await pool().fetch(
            "select embedding_model, count(*) n from doclib.chunks where tenant_id = $1 group by 1", tenant)
        return {"models": [
            {"role": "Text", "model": text.backend, "endpoint": text.endpoint, "state": "built in" if not text.endpoint else "configured"},
            {"role": "Vision", "model": vlm.model if vlm.enabled else None, "endpoint": vlm.endpoint if vlm.enabled else None,
             "state": "off" if not vlm.enabled else "reachable" if await vlm_reachable(vlm) else "unreachable"},
            {"role": "Embedding", "model": embeddings.model, "endpoint": embeddings.endpoint,
             "state": "reachable" if await embedding_ok() else "unreachable"},
        ], "chunks_by_model": {r["embedding_model"]: r["n"] for r in chunks}}

    return router
