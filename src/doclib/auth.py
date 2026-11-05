"""Caller identity from the API gateway (spec section 10.6). Every query is filtered by its tenant."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import jwt

from .config import AuthConfig


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    caller: str
    scopes: frozenset[str] = frozenset()


class AuthError(Exception):
    pass


class Authenticator:
    def __init__(self, config: AuthConfig, default_tenant: str) -> None:
        self.config = config
        self.default_tenant = default_tenant
        self.secret: str | None = None
        if config.mode == "bearer":
            if not config.token_secret_file:
                raise ValueError("auth.mode=bearer requires auth.token_secret_file")
            self.secret = Path(config.token_secret_file).read_text(encoding="utf-8").strip()

    def identify(self, headers: dict[str, str]) -> Identity:
        """`headers` must have lower-case names."""
        if self.config.mode == "none":
            return Identity(self.default_tenant, "anonymous")
        if self.config.mode == "header":
            # Only safe behind the gateway's mTLS: the network must not let others reach this port.
            tenant = headers.get("x-tenant-id")
            if not tenant:
                raise AuthError("missing X-Tenant-Id")
            return Identity(tenant, headers.get("x-caller-id", "unknown"))
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthError("missing bearer token")
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"], audience=self.config.token_audience,
                                options={"require": ["exp", "tenant_id"], "verify_aud": bool(self.config.token_audience)})
        except jwt.PyJWTError as exc:
            raise AuthError("invalid token") from exc
        scope = claims.get("scope", "")
        scopes = frozenset(scope.split() if isinstance(scope, str) else scope)
        return Identity(str(claims["tenant_id"]), str(claims.get("sub", "unknown")), scopes)
