"""Pluggable text extraction backends.

To add one, implement `TextBackend` and either add it to BUILTIN below or point config at it directly:

    extraction:
      text:
        backend: my_package.my_module:MyBackend

Nothing outside this package depends on a specific backend. The router, chunker and block model only see
Markdown per page, and tests/unit/test_backend_contract.py checks any backend against that contract.
"""

from __future__ import annotations

import importlib

from ...config import ExtractionConfig
from .base import BackendError, RawDocument, RawPage, TextBackend

BUILTIN = {
    "xberg": "doclib.extract.backends.xberg_backend:XbergBackend",
    "http": "doclib.extract.backends.http_backend:HttpBackend",
}
_instances: dict[str, TextBackend] = {}


def create_backend(config: ExtractionConfig) -> TextBackend:
    path = BUILTIN.get(config.text.backend, config.text.backend)
    module_name, _, class_name = path.partition(":")
    if not class_name:
        raise ValueError(f"unknown text backend {config.text.backend!r}; use one of {sorted(BUILTIN)} or module:Class")
    return getattr(importlib.import_module(module_name), class_name)(config)


def get_backend(config: ExtractionConfig) -> TextBackend:
    """One shared instance per configuration, because a process only ever uses one configuration."""
    key = config.model_dump_json()
    if key not in _instances:
        _instances[key] = create_backend(config)
    return _instances[key]


__all__ = ["BackendError", "RawDocument", "RawPage", "TextBackend", "create_backend", "get_backend"]
