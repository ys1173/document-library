"""Read-only source access through rclone (spec section 5).

Only allowlisted subcommands can run, so write operations are impossible by construction here.
Credentials should also be read-only at the source wherever it supports that.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import SourceConfig
from .log import event

READ_ONLY_SUBCOMMANDS = frozenset({"lsjson", "cat", "version"})
HASH_PREFERENCE = ("sha256", "sha1", "md5", "quickxorhash", "whirlpool", "crc32")
# Exit codes rclone uses for "directory not found" and "file not found".
NOT_FOUND_EXIT_CODES = {3, 4}
_STREAM_LIMIT = 16 * 1024 * 1024


class RcloneError(Exception):
    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class WriteAttemptError(RcloneError):
    """Raised before spawning if anything tries to run a non-allowlisted subcommand."""


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    modtime: datetime | None
    hashes: dict[str, str] = field(default_factory=dict)
    mime_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_modtime(value: str | None) -> datetime | None:
    if not value:
        return None
    # rclone emits nanoseconds and sometimes 'Z'. Python keeps microseconds, so trim the fraction first.
    text = re.sub(r"\.(\d{1,6})\d*", r".\1", value.replace("Z", "+00:00"))
    return datetime.fromisoformat(text).astimezone(UTC)


def parse_entry(raw: dict[str, Any]) -> Entry:
    return Entry(
        path=raw["Path"],
        size=int(raw.get("Size", 0)),
        modtime=parse_modtime(raw.get("ModTime")),
        hashes={k.lower(): v for k, v in (raw.get("Hashes") or {}).items() if v},
        mime_type=raw.get("MimeType"),
        metadata=raw.get("Metadata") or {},
    )


def best_hash(hashes: dict[str, str]) -> tuple[str, str] | None:
    for name in HASH_PREFERENCE:
        if hashes.get(name):
            return name, hashes[name]
    for name in sorted(hashes):
        return name, hashes[name]
    return None


def content_version(entry: Entry) -> str:
    """Stable version of a file: its hash, else size+modtime (spec section 5.1)."""
    picked = best_hash(entry.hashes)
    if picked:
        return f"{picked[0]}:{picked[1]}"
    micros = int(entry.modtime.timestamp() * 1_000_000) if entry.modtime else 0
    return f"sm:{entry.size}-{micros}"


def acl_from_metadata(metadata: dict[str, Any]) -> list[dict[str, str]] | None:
    """Capture permissions where the backend exposes them. Stored, not enforced in v1 (spec section 12)."""
    raw = metadata.get("permissions")
    if not raw:
        return None
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    acl: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        grantee = item.get("grantedToV2") or item.get("grantedTo") or {}
        who = grantee.get("user") or grantee.get("group") or {} if isinstance(grantee, dict) else {}
        principal = (who.get("email") or who.get("displayName") or who.get("id")
                     or item.get("emailAddress") or item.get("id") or "unknown")
        roles = item.get("roles") or [item.get("role") or "unknown"]
        acl.append({"principal": str(principal), "type": str(item.get("type") or "user"),
                    "permission": ",".join(str(r) for r in roles)})
    return acl or None


def remote_name(source: SourceConfig) -> str:
    """rclone remote for a source. Declarative sources get a name derived from their id."""
    if source.rclone_remote:
        return source.rclone_remote
    return "dl_" + re.sub(r"[^a-z0-9]", "_", source.id.lower())


class Rclone:
    def __init__(self, binary: str = "rclone", config_file: str | None = None) -> None:
        self.binary = binary
        self.config_file = config_file
        self._obscured: dict[str, str] = {}
        self._passwords: dict[str, set[str]] | None = None

    def _base_env(self) -> dict[str, str]:
        return {
            **os.environ,
            # Ask the backends that support it to expose permissions in metadata, read-only.
            "RCLONE_ONEDRIVE_METADATA_PERMISSIONS": "read",
            "RCLONE_DRIVE_METADATA_PERMISSIONS": "read",
        }

    async def catalogue(self) -> list[dict[str, Any]]:
        """rclone's own list of backends and options. A fixed read-only command, not a general `config` call."""
        proc = await asyncio.create_subprocess_exec(
            self.binary, "config", "providers", env=self._base_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RcloneError(f"could not read the rclone catalogue: {err.decode(errors='replace')[-200:]}", proc.returncode)
        return json.loads(out)

    async def _obscure(self, value: str) -> str:
        """rclone requires password-type options in its obscured form. The value goes over stdin, not argv."""
        if value not in self._obscured:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "obscure", "-", env=self._base_env(), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate(value.encode())
            if proc.returncode != 0:
                raise RcloneError("could not prepare a password credential", proc.returncode)
            self._obscured[value] = out.decode().strip()
        return self._obscured[value]

    async def env_for(self, source: SourceConfig | None) -> dict[str, str]:
        """The subprocess environment. Connector settings and secrets travel in env vars, so never in argv or `ps`."""
        env = self._base_env()
        if source is None or not source.type:
            return env
        prefix = f"RCLONE_CONFIG_{remote_name(source).upper()}_"
        env[f"{prefix}TYPE"] = source.type
        for key, value in source.options.items():
            env[f"{prefix}{key.upper()}"] = str(value).lower() if isinstance(value, bool) else str(value)
        passwords = await self._password_options(source.type)
        for option, variable in source.credentials.items():
            value = os.environ.get(variable)
            if value is None:
                raise RcloneError(f"credential environment variable is not set: {variable}")
            env[f"{prefix}{option.upper()}"] = await self._obscure(value) if option in passwords else value
        return env

    async def _password_options(self, backend: str) -> set[str]:
        if self._passwords is None:
            self._passwords = {e["Prefix"]: {o["Name"] for o in e["Options"] if o.get("IsPassword")}
                               for e in await self.catalogue()}
        return self._passwords.get(backend, set())

    def _argv(self, subcommand: str, args: list[str], source: SourceConfig | None) -> list[str]:
        if subcommand not in READ_ONLY_SUBCOMMANDS:
            raise WriteAttemptError(f"rclone subcommand not allowed: {subcommand}")
        argv = [self.binary, subcommand, *args]
        if self.config_file:
            argv += ["--config", self.config_file]
        if source and source.tps_limit:
            argv += ["--tpslimit", str(source.tps_limit)]
        if source and source.checkers:
            argv += ["--checkers", str(source.checkers)]
        return argv

    @staticmethod
    def target(source: SourceConfig, path: str = "") -> str:
        root = source.root_path.rstrip("/") or ("/" if source.root_path.startswith("/") else "")
        joined = f"{root}/{path}" if path and root != "/" else (f"/{path}" if path else root)
        return joined if source.rclone_remote == "local" else f"{remote_name(source)}:{joined}"

    async def list_files(self, source: SourceConfig) -> AsyncIterator[Entry]:
        """Stream a recursive listing. Raises RcloneError if the listing did not complete."""
        args = [self.target(source), "--recursive", "--files-only", "--metadata", "--fast-list"]
        if source.use_hash:
            args.append("--hash")
        proc = await asyncio.create_subprocess_exec(
            *self._argv("lsjson", args, source), env=await self.env_for(source), limit=_STREAM_LIMIT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stderr_task = asyncio.create_task(proc.stderr.read())
        try:
            while line := await proc.stdout.readline():
                text = line.strip().rstrip(b",")
                if text in (b"[", b"]", b""):
                    continue
                yield parse_entry(json.loads(text))
            returncode = await proc.wait()
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            raise
        stderr = (await stderr_task).decode(errors="replace")
        if returncode != 0:
            raise RcloneError(f"lsjson failed ({returncode}): {stderr[-300:]}", returncode)

    async def probe(self, source: SourceConfig, timeout_s: float) -> None:
        """Connectivity check: list the root once, without recursing. Raises RcloneError if it fails."""
        proc = await asyncio.create_subprocess_exec(
            *self._argv("lsjson", [self.target(source), "--max-depth", "1", "--no-modtime", "--no-mimetype"], source),
            env=await self.env_for(source), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RcloneError(f"no response within {timeout_s:.0f} s") from exc
        if proc.returncode != 0:
            raise RcloneError(err.decode(errors="replace").strip()[-300:] or "listing failed", proc.returncode)

    async def stat(self, source: SourceConfig, path: str) -> Entry | None:
        args = [self.target(source, path), "--stat", "--metadata"]
        if source.use_hash:
            args.append("--hash")
        proc = await asyncio.create_subprocess_exec(
            *self._argv("lsjson", args, source), env=await self.env_for(source),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode in NOT_FOUND_EXIT_CODES:
            return None
        if proc.returncode != 0:
            raise RcloneError(f"stat failed ({proc.returncode}): {err.decode(errors='replace')[-300:]}",
                              proc.returncode)
        out = out.strip()
        if not out or out == b"null":
            return None
        raw = json.loads(out)
        raw["Path"] = path
        return parse_entry(raw)

    async def cat(self, source: SourceConfig, path: str, max_bytes: int) -> bytes:
        """Read a file into memory. Nothing is written to disk."""
        proc = await asyncio.create_subprocess_exec(
            *self._argv("cat", [self.target(source, path)], source), env=await self.env_for(source), limit=_STREAM_LIMIT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stderr_task = asyncio.create_task(proc.stderr.read())
        chunks: list[bytes] = []
        total = 0
        try:
            while chunk := await proc.stdout.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise RcloneError(f"file exceeds the {max_bytes} byte limit")
                chunks.append(chunk)
            returncode = await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        stderr = (await stderr_task).decode(errors="replace")
        if returncode != 0:
            raise RcloneError(f"cat failed ({returncode}): {stderr[-300:]}", returncode)
        event("rclone.read", level=10, source_id=source.id, size_bytes=total)
        return b"".join(chunks)
