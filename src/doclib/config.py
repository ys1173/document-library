"""YAML configuration with environment overrides. Secrets come from files, never inline."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_EXTENSIONS = [
    # P1
    "pdf", "docx", "doc", "xlsx", "xls", "csv", "pptx", "ppt", "txt", "md", "html", "htm",
    # P2
    "eml", "msg", "rtf", "odt", "ods", "odp", "json", "yaml", "yml", "xml", "log", "cfg", "conf", "ini",
    # Images
    "png", "jpg", "jpeg", "tif", "tiff",
]  # fmt: skip

DEFAULT_EXCLUDE = ["**/~$*", "**/.DS_Store", "**/Thumbs.db"]


def parse_duration(value: str | float) -> float:
    """Parse '15m', '90s', '2h' or a number of seconds."""
    if isinstance(value, int | float):
        return float(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*", str(value))
    if not match:
        raise ValueError(f"invalid duration: {value!r}")
    unit = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}[match.group(2)]
    return float(match.group(1)) * unit


def _read_secret(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PostgresConfig(Strict):
    dsn_file: str | None = None
    dsn: str | None = None  # dev/test only; production uses dsn_file or DOCLIB_POSTGRES_DSN_FILE
    pool_min: int = 1
    pool_max: int = 10

    def resolve_dsn(self) -> str:
        if self.dsn_file:
            return _read_secret(self.dsn_file)
        if self.dsn:
            return self.dsn
        raise ValueError("postgres.dsn_file (or postgres.dsn in dev) is required")


class SourceConfig(Strict):
    """A place to index. Either declarative (`type` + `options` + `credentials`) or a named rclone.conf remote."""

    id: str
    root_path: str
    # Declarative connector: any rclone backend, with no rclone.conf. Credentials are env var NAMES, never values.
    type: str | None = None  # an rclone backend, for example s3, smb, webdav, sftp, onedrive
    options: dict[str, str | int | float | bool] = Field(default_factory=dict)
    credentials: dict[str, str] = Field(default_factory=dict)  # rclone option -> environment variable name
    # Or an existing remote in rclone.conf, or "local" for a plain filesystem path.
    rclone_remote: str | None = None
    tenant_id: str | None = None
    include: list[str] = Field(default_factory=lambda: ["**"])
    exclude: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    tags: list[str] = Field(default_factory=list)
    path_tags: dict[str, list[str]] = Field(default_factory=dict)  # path prefix -> tags
    scan_interval: float = 900.0
    max_file_size_mb: int = 200
    allowed_extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    max_delete_ratio: float = 0.5
    tps_limit: float | None = None  # rclone --tpslimit, for throttled sources
    checkers: int | None = None  # rclone --checkers
    use_hash: bool = True  # hashing can be expensive on sources without stored hashes

    _dur = field_validator("scan_interval", mode="before")(parse_duration)

    @model_validator(mode="after")
    def _one_way_to_connect(self) -> SourceConfig:
        if bool(self.type) == bool(self.rclone_remote):
            raise ValueError("set exactly one of `type` (declarative) or `rclone_remote`")
        return self


class ConnectorPolicy(Strict):
    """Limits on what callers of the admin API may create."""

    # Local-disk and wrapper backends are excluded: they could read this service's own secrets and files.
    allowed_types: list[str] = Field(default_factory=lambda: [
        "s3", "smb", "webdav", "sftp", "ftp", "onedrive", "drive", "azureblob", "azurefiles", "gcs",
        "b2", "box", "dropbox", "hdfs", "swift", "oos", "storj",
    ])  # fmt: skip
    # Credentials may only name variables with this prefix. Otherwise a caller could point an endpoint at their
    # own server and ask for DOCLIB_POSTGRES_PASSWORD as a "credential".
    credential_env_prefix: str = "DOCLIB_CRED_"
    test_timeout_s: float = 30


class RouterConfig(Strict):
    min_chars_per_page: int = 50
    image_area_ratio: float = 0.6
    garbled_ratio: float = 0.05


class VlmConfig(Strict):
    """Vision model behind any OpenAI-compatible endpoint. Local CPU or remote GPU differ only in `endpoint`."""

    enabled: bool = False
    endpoint: str | None = None  # http://vision:8000/v1 locally, https://gpu-host/v1 remotely
    api_key_file: str | None = None
    model: str = "zai-org/GLM-OCR"
    prompt: str = "Text Recognition:"  # the prompt template is configuration, so models can be swapped
    max_tokens: int = 4096
    concurrency: int = 1  # raise on GPU; keep at 1 on CPU
    timeout_s: float = 900  # CPU pages took 60-170 s when measured
    retries: int = 2
    render_dpi: int = 170


class TextBackendConfig(Strict):
    """Which text extractor to use. Swapping it is a config change plus `doclib reindex --all`."""

    backend: str = "xberg"  # xberg | http | my_package.my_module:MyBackend
    endpoint: str | None = None  # for http, for example http://extract:8090/v1
    api_key_file: str | None = None


class ExtractionConfig(Strict):
    text: TextBackendConfig = Field(default_factory=TextBackendConfig)
    router: RouterConfig = Field(default_factory=RouterConfig)
    vlm: VlmConfig = Field(default_factory=VlmConfig)
    ocr_fallback: str = "tesseract"
    ocr_languages: list[str] = Field(default_factory=lambda: ["eng", "jpn"])
    timeout_s: float = 600


class ChunkingConfig(Strict):
    target_tokens: int = 600
    min_tokens: int = 400
    max_tokens: int = 800
    overlap_tokens: int = 50
    max_block_tokens: int = 2000
    spreadsheet_row_group: int = 200


class EmbeddingsConfig(Strict):
    endpoint: str
    model: str = "BAAI/bge-m3"
    dimension: int = 1024
    batch_size: int = 64
    concurrency: int = 2
    timeout_s: float = 120
    api_key_file: str | None = None
    # Some models expect a marker on each text, for example E5's "query: " and "passage: ". BGE-M3 needs none.
    document_prefix: str = ""
    query_prefix: str = ""


class RerankConfig(Strict):
    enabled: bool = False
    endpoint: str | None = None


class SearchConfig(Strict):
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    candidates: int = 50
    rrf_k: int = 60
    ef_search: int = 100
    open_doc_max_chars: int = 20000


class WebhookConfig(Strict):
    enabled: bool = False
    url: str | None = None  # dev/test only; URLs often carry tokens, so prefer url_file
    url_file: str | None = None
    secret_file: str | None = None
    digest_max_wait: float = 300.0
    max_changes_per_message: int = 500
    timeout_s: float = 15
    max_attempts: int = 20
    max_backoff_s: float = 3600
    event_retention_days: int = 30

    _dur = field_validator("digest_max_wait", mode="before")(parse_duration)

    def resolve_url(self) -> str | None:
        if self.url_file:
            return _read_secret(self.url_file)
        return self.url

    def resolve_secret(self) -> str | None:
        return _read_secret(self.secret_file) if self.secret_file else None


class NotificationsConfig(Strict):
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)


class LoggingConfig(Strict):
    level: str = "INFO"
    file: str | None = None
    file_max_mb: int = 100
    file_backups: int = 5


class TelemetryConfig(Strict):
    otlp_endpoint: str | None = None


class AuthConfig(Strict):
    # none: dev only. header: trust X-Tenant-Id and X-Caller-Id set by the gateway over mTLS. bearer: signed HS256 token.
    mode: str = "none"
    token_secret_file: str | None = None
    token_audience: str | None = None

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in {"none", "header", "bearer"}:
            raise ValueError("auth.mode must be none, header or bearer")
        return v


class ConnectionProfile(Strict):
    """A destination an operator approves for agents. They choose the profile and a folder, and nothing else about it."""

    description: str = ""
    type: str
    options: dict[str, str | int | float | bool] = Field(default_factory=dict)
    credentials: dict[str, str] = Field(default_factory=dict)  # rclone option -> environment variable NAME
    allowed_roots: list[str] = Field(min_length=1)  # Folders agents may index, or ["*"] for the whole store.
    scan_interval: str | int = "15m"


class AdminMcpConfig(Strict):
    """MCP tools on the admin port for a trusted setup agent. Off unless turned on."""

    enabled: bool = False
    profiles: dict[str, ConnectionProfile] = Field(default_factory=dict)


class PortalConfig(Strict):
    """The web portal on the admin port. It asks for a login only if DOCLIB_PORTAL_PASSWORD is set."""

    enabled: bool = True


class AdminConfig(Strict):
    """The service-to-service admin API (connectors). Separate from the agent-facing API, on its own port."""

    host: str = "0.0.0.0"
    port: int = 8081
    auth: AuthConfig = Field(default_factory=AuthConfig)
    required_scope: str = "doclib:admin"  # In bearer mode, tokens must carry this in their `scope` claim.
    mcp: AdminMcpConfig = Field(default_factory=AdminMcpConfig)
    portal: PortalConfig = Field(default_factory=PortalConfig)

    @model_validator(mode="after")
    def _mcp_needs_tokens(self) -> AdminConfig:
        # Only bearer tokens carry the admin scope, so that's the only mode that can tell a setup agent from any other.
        if self.mcp.enabled and self.auth.mode != "bearer":
            raise ValueError("admin.mcp.enabled requires admin.auth.mode: bearer")
        return self


class WorkerConfig(Strict):
    concurrency: int = 2
    max_attempts: int = 5
    lease_s: float = 120  # a killed worker's job is reclaimed after this; live jobs heartbeat
    poll_s: float = 2
    vlm_priority: int = 500  # always behind index jobs, so the vision model can't block the fast path
    backoff_base_s: float = 30


class ApiConfig(Strict):
    host: str = "0.0.0.0"
    port: int = 8080


class Settings(Strict):
    tenant_id: str = "default"
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    rclone_binary: str = "rclone"
    rclone_config_file: str | None = None
    tombstone_days: int = 30
    sources: list[SourceConfig] = Field(default_factory=list)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    embeddings: EmbeddingsConfig
    search: SearchConfig = Field(default_factory=SearchConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    connectors: ConnectorPolicy = Field(default_factory=ConnectorPolicy)


def _apply_env(data: dict[str, Any], environ: dict[str, str]) -> None:
    """DOCLIB_POSTGRES__DSN_FILE=/x sets postgres.dsn_file. Values are parsed as YAML scalars."""
    for key, raw in environ.items():
        if not key.startswith("DOCLIB_") or "__" not in key or key.startswith("DOCLIB_TEST"):
            continue
        parts = key.removeprefix("DOCLIB_").lower().split("__")
        node = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = yaml.safe_load(raw)


def load_settings(path: str | None = None, environ: dict[str, str] | None = None) -> Settings:
    environ = dict(os.environ) if environ is None else environ
    path = path or environ.get("DOCLIB_CONFIG") or "config.yaml"
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    _apply_env(data, environ)
    return Settings.model_validate(data)
