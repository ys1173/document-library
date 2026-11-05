"""doclib-admin: the service-to-service API for other parts of the platform to manage connectors.

This is deliberately separate from the agent-facing API (spec section 16): its own port and its own auth. Agents
search and read there. Only platform services use these REST routes, and a trusted setup agent can get the narrower
tools in admin_mcp.py if an operator turns them on. The OpenAPI schema is at /openapi.json.
"""


import contextlib
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .admin_mcp import McpAdminGuard, build_admin_mcp
from .auth import Authenticator, AuthError, Identity
from .config import Settings
from .connectors import (
    CheckResult,
    Connector,
    ConnectorError,
    ConnectorService,
    ConnectorSpec,
    backend_detail,
    backend_summary,
)
from .db import create_pool
from .log import event
from .rclone import Rclone

ConnectorId = Annotated[str, Path(description="Lowercase letters, digits and hyphens.")]


class CreateRequest(ConnectorSpec):
    id: str


class ValidateResponse(BaseModel):
    valid: bool
    connection: CheckResult


class DeleteResponse(BaseModel):
    files_removed: int
    chunks_removed: int


class ScanResponse(BaseModel):
    scan_requested: bool = True


def create_admin_app(settings: Settings) -> FastAPI:
    state: dict[str, ConnectorService] = {}
    authenticator = Authenticator(settings.admin.auth, settings.tenant_id)
    if settings.admin.auth.mode == "none":
        event("admin.unauthenticated", level=30, error="admin.auth.mode is none: use this only for local development")
    mcp = build_admin_mcp(lambda: state["service"], settings.admin.mcp) if settings.admin.mcp.enabled else None

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        pool = await create_pool(settings)
        state["service"] = ConnectorService(pool, Rclone(settings.rclone_binary, settings.rclone_config_file), settings.connectors)
        event("admin.started", auth_mode=settings.admin.auth.mode, mcp_enabled=mcp is not None)
        if mcp:
            async with mcp.session_manager.run():
                yield
        else:
            yield
        await pool.close()

    app = FastAPI(title="Document Library: admin", version="1", lifespan=lifespan,
                  description="Create and manage source connectors. Any allowed rclone backend works with a declarative call.")

    def authorise(headers: dict[str, str], path: str) -> Identity:
        try:
            found = authenticator.identify(headers)
        except AuthError as exc:
            event("admin.auth_rejected", level=30, reason=str(exc), path=path)
            raise ConnectorError("unauthorized", "authentication required", status=401) from exc
        if settings.admin.auth.mode == "bearer" and settings.admin.required_scope not in found.scopes:
            event("admin.scope_rejected", level=30, caller=found.caller, path=path)
            raise ConnectorError("forbidden", f"token lacks the {settings.admin.required_scope} scope", status=403)
        return found

    def identity(request: Request) -> Identity:
        return authorise({k.lower(): v for k, v in request.headers.items()}, request.url.path)

    Caller = Annotated[Identity, Depends(identity)]

    def service() -> ConnectorService:
        return state["service"]

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        if request.url.path != "/healthz":
            event("admin.request", method=request.method, path=request.url.path, status=response.status_code,
                  duration_ms=round((time.monotonic() - started) * 1000, 1))
        return response

    @app.exception_handler(ConnectorError)
    async def _connector_error(_: Request, exc: ConnectorError) -> JSONResponse:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, "details": exc.details}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors()]
        return JSONResponse({"error": {"code": "invalid_request", "message": "the request is not valid", "details": details}},
                            status_code=422)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        await service().pool.fetchval("select 1")
        return {"status": "ok"}

    @app.get("/v1/connector-types", tags=["catalogue"], summary="Types of connector available")
    async def list_types(_: Caller) -> dict[str, list[dict[str, Any]]]:
        return {"types": [backend_summary(e) for _, e in sorted((await service().catalogue()).items())]}

    @app.get("/v1/connector-types/{type_name}", tags=["catalogue"], summary="Options for one type, from rclone")
    async def get_type(type_name: str, _: Caller, advanced: Annotated[bool, Query()] = False) -> dict[str, Any]:
        entry = (await service().catalogue()).get(type_name)
        if entry is None:
            raise ConnectorError("unsupported_type", f"type {type_name!r} is not available", status=404)
        detail = backend_detail(entry)
        if not advanced:
            detail["options"] = [o for o in detail["options"] if not o["advanced"]]
        return detail

    @app.get("/v1/connectors", tags=["connectors"], summary="List connectors")
    async def list_connectors(caller: Caller) -> dict[str, list[Connector]]:
        return {"connectors": await service().list(caller.tenant_id)}

    @app.post("/v1/connectors", tags=["connectors"], status_code=201, summary="Create a connector. Scanning starts straight away.")
    async def create_connector(body: CreateRequest, caller: Caller,
                               test: Annotated[bool, Query(description="Check the source is readable first")] = True) -> Connector:
        spec = ConnectorSpec.model_validate(body.model_dump(exclude={"id"}))
        connector, _ = await service().apply(body.id, spec, caller.tenant_id, create_only=True, test=test, caller=caller.caller)
        return connector

    @app.post("/v1/connectors/validate", tags=["connectors"], summary="Check a definition and its connection, storing nothing")
    async def validate_connector(body: CreateRequest, caller: Caller) -> ValidateResponse:
        spec = ConnectorSpec.model_validate(body.model_dump(exclude={"id"}))
        source = await service().validate(body.id, spec, caller.tenant_id)
        connection = await service().test(source)
        return ValidateResponse(valid=True, connection=connection)

    @app.get("/v1/connectors/{connector_id}", tags=["connectors"], summary="A connector and its indexing status")
    async def get_connector(connector_id: ConnectorId, caller: Caller) -> Connector:
        return await service().get(connector_id, caller.tenant_id)

    @app.put("/v1/connectors/{connector_id}", tags=["connectors"], summary="Create or update to this state. Safe to repeat.")
    async def put_connector(connector_id: ConnectorId, spec: ConnectorSpec, caller: Caller, response: Response,
                            test: Annotated[bool, Query(description="Check the source is readable first")] = True) -> Connector:
        connector, created = await service().apply(connector_id, spec, caller.tenant_id, test=test, caller=caller.caller)
        response.status_code = 201 if created else 200
        return connector

    @app.delete("/v1/connectors/{connector_id}", tags=["connectors"], summary="Remove a connector and its index. Source files are untouched.")
    async def delete_connector(connector_id: ConnectorId, caller: Caller) -> DeleteResponse:
        return DeleteResponse(**await service().delete(connector_id, caller.tenant_id, caller=caller.caller))

    @app.post("/v1/connectors/{connector_id}/scan", tags=["connectors"], status_code=202, summary="Scan now, not at the next interval")
    async def scan_connector(connector_id: ConnectorId, caller: Caller) -> ScanResponse:
        await service().request_scan(connector_id, caller.tenant_id, caller=caller.caller)
        return ScanResponse()

    if mcp:
        app.mount("/", McpAdminGuard(mcp.streamable_http_app(), authorise))  # MCP at /mcp. Mounted last so REST routes win.
    return app
