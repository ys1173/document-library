"""Declarative connectors: validate against rclone's own catalogue, test, and store.

Any backend rclone supports works without new code here, because types and options come from `rclone config
providers`. Secrets are never stored: only the names of the environment variables that hold them.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import DEFAULT_EXCLUDE, DEFAULT_EXTENSIONS, ConnectorPolicy, SourceConfig, parse_duration
from .log import event
from .rclone import Rclone, RcloneError, remote_name

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$")
_OPTION = re.compile(r"^[a-z0-9_]+$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SECRET_NAME = re.compile(r"secret|pass|token|key|credential|sas_url|connection_string", re.IGNORECASE)


def is_secret(option: dict[str, Any]) -> bool:
    """rclone marks hosts and usernames `Sensitive` too, because it redacts them from logs. Those belong in options.

    A value is a secret if it's a password type, or Sensitive with a secret-style name (secret_access_key, token...).
    """
    return bool(option.get("IsPassword") or (option.get("Sensitive") and _SECRET_NAME.search(option["Name"])))


class ConnectorError(Exception):
    """A well-defined failure for API callers."""

    def __init__(self, code: str, message: str, status: int = 422, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


class ConnectorSpec(BaseModel):
    """Desired state of a connector. Unknown fields are rejected, so typos fail loudly."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(description="rclone backend, for example s3, smb, webdav. See GET /v1/connector-types.")
    root_path: str = Field(description="Folder or bucket to index, for example 'docs-bucket/operations'.")
    options: dict[str, str | int | float | bool] = Field(
        default_factory=dict, description="Non-secret rclone options, for example provider and endpoint.")
    credentials: dict[str, str] = Field(
        default_factory=dict, description="rclone option name -> name of the environment variable holding its value.")
    include: list[str] = Field(default_factory=lambda: ["**"])
    exclude: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    tags: list[str] = Field(default_factory=list)
    path_tags: dict[str, list[str]] = Field(default_factory=dict)
    scan_interval: str | int = "15m"
    max_file_size_mb: int = Field(default=200, gt=0)
    allowed_extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    tps_limit: float | None = Field(default=None, gt=0)
    checkers: int | None = Field(default=None, gt=0)
    use_hash: bool = True

    @field_validator("root_path")
    @classmethod
    def _root(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("root_path is required")
        if any(part == ".." for part in value.split("/")):
            raise ValueError("root_path must not contain '..'")
        return value

    @field_validator("scan_interval")
    @classmethod
    def _interval(cls, value: str | int) -> str | int:
        if parse_duration(value) < 5:
            raise ValueError("scan_interval must be at least 5s")
        return value


class CheckResult(BaseModel):
    ok: bool
    message: str | None = None


class ConnectorStatus(BaseModel):
    enabled: bool
    files_by_status: dict[str, int] = Field(default_factory=dict)
    last_scan_at: datetime | None = None
    last_scan_ok: bool | None = None
    last_scan_error: str | None = None
    last_success_at: datetime | None = None


class Connector(BaseModel):
    id: str
    tenant_id: str
    origin: str
    spec: ConnectorSpec | None = Field(description="Null for sources that use a named rclone.conf remote.")
    status: ConnectorStatus
    created_at: datetime
    updated_at: datetime


def backend_summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {"type": entry["Prefix"], "description": entry.get("Description", "")}


def backend_detail(entry: dict[str, Any]) -> dict[str, Any]:
    options = []
    for option in entry["Options"]:
        secret = is_secret(option)
        examples = [e.get("Value") for e in (option.get("Examples") or []) if e.get("Value")]
        options.append({"name": option["Name"], "required": bool(option.get("Required")), "secret": secret,
                        "advanced": bool(option.get("Advanced")), "help": (option.get("Help") or "").split("\n")[0][:200],
                        "examples": examples[:40]})
    return {**backend_summary(entry), "options": options}


class ConnectorService:
    def __init__(self, pool: asyncpg.Pool, rclone: Rclone, policy: ConnectorPolicy) -> None:
        self.pool = pool
        self.rclone = rclone
        self.policy = policy
        self._catalogue: dict[str, dict[str, Any]] | None = None

    async def catalogue(self) -> dict[str, dict[str, Any]]:
        """Backends this deployment allows, from rclone itself."""
        if self._catalogue is None:
            entries = await self.rclone.catalogue()
            allowed = set(self.policy.allowed_types)
            self._catalogue = {e["Prefix"]: e for e in entries if e["Prefix"] in allowed}
        return self._catalogue

    async def validate(self, connector_id: str, spec: ConnectorSpec, tenant_id: str) -> SourceConfig:
        """Everything that can be checked without touching the source."""
        if not _ID.match(connector_id):
            raise ConnectorError("invalid_id", "id must be 2-63 lowercase letters, digits or hyphens")
        catalogue = await self.catalogue()
        entry = catalogue.get(spec.type)
        if entry is None:
            raise ConnectorError("unsupported_type", f"type {spec.type!r} is not available",
                                 details={"supported": sorted(catalogue)})
        options = {o["Name"]: o for o in entry["Options"]}
        problems: list[str] = []
        for name in spec.options:
            if not _OPTION.match(name) or name not in options:
                problems.append(f"options.{name}: not an option of the {spec.type} backend")
            elif is_secret(options[name]):
                problems.append(f"options.{name}: this is a secret, so pass it through credentials, not options")
        for name, variable in spec.credentials.items():
            if name not in options:
                problems.append(f"credentials.{name}: not an option of the {spec.type} backend")
            if not _ENV_NAME.match(variable) or not variable.startswith(self.policy.credential_env_prefix):
                problems.append(f"credentials.{name}: variable must start with {self.policy.credential_env_prefix}")
            elif variable not in os.environ:
                problems.append(f"credentials.{name}: environment variable {variable} is not set on this service")
        given = set(spec.options) | set(spec.credentials)
        for name, option in options.items():
            if option.get("Required") and not option.get("Advanced") and name not in given:
                problems.append(f"{name}: required by the {spec.type} backend")
        if problems:
            raise ConnectorError("invalid_connector", "the connector definition is not valid", details=problems)
        return SourceConfig.model_validate({"id": connector_id, "tenant_id": tenant_id, **spec.model_dump()})

    async def test(self, source: SourceConfig) -> CheckResult:
        """Can we actually read the source? Uses a read-only listing."""
        try:
            await self.rclone.probe(source, self.policy.test_timeout_s)
            return CheckResult(ok=True)
        except RcloneError as exc:
            return CheckResult(ok=False, message=self._redact(str(exc), source))

    @staticmethod
    def _redact(message: str, source: SourceConfig) -> str:
        for variable in source.credentials.values():
            value = os.environ.get(variable)
            if value:
                message = message.replace(value, "***")
        return message

    async def _row(self, connector_id: str, tenant_id: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            """
            select s.*, coalesce((select jsonb_object_agg(status, n) from
              (select status, count(*) n from doclib.files where source_id = s.source_id group by status) c), '{}') counts
            from doclib.sources s where s.source_id = $1 and s.tenant_id = $2
            """, connector_id, tenant_id)

    @staticmethod
    def _to_connector(row: asyncpg.Record) -> Connector:
        config = row["config"]
        spec = None
        if config.get("type"):
            spec = ConnectorSpec.model_validate({k: v for k, v in config.items() if k in ConnectorSpec.model_fields})
        status = ConnectorStatus(enabled=row["enabled"], files_by_status=row["counts"], last_scan_at=row["last_scan_at"],
                                 last_scan_ok=row["last_scan_ok"], last_scan_error=row["last_scan_error"],
                                 last_success_at=row["last_success_at"])
        return Connector(id=row["source_id"], tenant_id=row["tenant_id"], origin=row["origin"], spec=spec, status=status,
                         created_at=row["created_at"], updated_at=row["updated_at"])

    async def get(self, connector_id: str, tenant_id: str) -> Connector:
        row = await self._row(connector_id, tenant_id)
        if row is None:
            raise ConnectorError("not_found", f"no connector {connector_id!r}", status=404)
        return self._to_connector(row)

    async def list(self, tenant_id: str) -> list[Connector]:
        rows = await self.pool.fetch("select source_id from doclib.sources where tenant_id = $1 order by source_id", tenant_id)
        return [await self.get(r["source_id"], tenant_id) for r in rows]

    async def apply(self, connector_id: str, spec: ConnectorSpec, tenant_id: str, *, create_only: bool = False,
                    test: bool = True, caller: str = "unknown") -> tuple[Connector, bool]:
        """Create or update to the desired state. Returns the connector, and whether it was created."""
        source = await self.validate(connector_id, spec, tenant_id)
        if test:
            result = await self.test(source)
            if not result.ok:
                raise ConnectorError("connection_failed", "could not read the source", details=result.message)
        config = source.model_dump(mode="json")
        async with self.pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                "select tenant_id, origin, config from doclib.sources where source_id = $1 for update", connector_id)
            if existing is not None:
                # Ids are global. Say "exists" only to its own tenant, and don't reveal other tenants' ids.
                if existing["tenant_id"] != tenant_id or create_only or existing["origin"] != "api":
                    raise ConnectorError("already_exists", f"connector id {connector_id!r} is not available", status=409)
                if existing["config"] != config:
                    await conn.execute(
                        "update doclib.sources set rclone_remote = $2, root_path = $3, config = $4, enabled = true, "
                        "scan_requested_at = now(), updated_at = now() where source_id = $1",
                        connector_id, remote_name(source), source.root_path, config)
            else:
                await conn.execute(
                    "insert into doclib.sources (source_id, tenant_id, rclone_remote, root_path, config, origin, "
                    "scan_requested_at) values ($1, $2, $3, $4, $5, 'api', now())",
                    connector_id, tenant_id, remote_name(source), source.root_path, config)
        created = existing is None
        event("connector.applied", connector_id=connector_id, tenant_id=tenant_id, type=spec.type, newly_created=created, caller=caller)
        return await self.get(connector_id, tenant_id), created

    async def delete(self, connector_id: str, tenant_id: str, *, caller: str = "unknown") -> dict[str, int]:
        """Remove a connector and its index. The source files are untouched, and the index can always be rebuilt."""
        row = await self.pool.fetchrow(
            "select origin from doclib.sources where source_id = $1 and tenant_id = $2", connector_id, tenant_id)
        if row is None:
            raise ConnectorError("not_found", f"no connector {connector_id!r}", status=404)
        if row["origin"] != "api":
            raise ConnectorError("managed_by_config", "this source comes from the config file; remove it there", status=409)
        async with self.pool.acquire() as conn, conn.transaction():
            files = await conn.fetchval("select count(*) from doclib.files where source_id = $1", connector_id)
            chunks = await conn.fetchval(
                "select count(*) from doclib.chunks c join doclib.files f using (file_id) where f.source_id = $1", connector_id)
            await conn.execute("delete from doclib.files where source_id = $1", connector_id)  # Cascades to chunks and jobs.
            await conn.execute("delete from doclib.sources where source_id = $1", connector_id)
        event("connector.deleted", connector_id=connector_id, tenant_id=tenant_id, files=files, chunks=chunks, caller=caller)
        return {"files_removed": files, "chunks_removed": chunks}

    async def request_scan(self, connector_id: str, tenant_id: str, *, caller: str = "unknown") -> None:
        result = await self.pool.execute(
            "update doclib.sources set scan_requested_at = now() where source_id = $1 and tenant_id = $2", connector_id, tenant_id)
        if result.endswith(" 0"):
            raise ConnectorError("not_found", f"no connector {connector_id!r}", status=404)
        event("connector.scan_requested", connector_id=connector_id, tenant_id=tenant_id, caller=caller)


__all__ = ["CheckResult", "Connector", "ConnectorError", "ConnectorService", "ConnectorSpec"]
