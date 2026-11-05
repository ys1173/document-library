import json
import logging
import os
import time

import jwt
import pytest

from doclib.auth import Authenticator, AuthError
from doclib.config import AuthConfig, Settings, load_settings, parse_duration
from doclib.library import group_adjacent, rrf_merge
from doclib.log import DocumentContentFilter, JsonFormatter
from doclib.notify import build_payloads, sign, summarise, summary_text
from doclib.rclone import (
    READ_ONLY_SUBCOMMANDS,
    Rclone,
    WriteAttemptError,
    acl_from_metadata,
    parse_entry,
    parse_modtime,
)


@pytest.mark.parametrize("subcommand", ["sync", "copy", "move", "delete", "mount", "purge", "rcat", "touch", "config"])
def test_write_subcommands_cannot_run(subcommand):
    with pytest.raises(WriteAttemptError):
        Rclone()._argv(subcommand, [], None)


def test_only_read_commands_are_allowed():
    assert READ_ONLY_SUBCOMMANDS == {"lsjson", "cat", "version"}


def test_modtime_with_nanoseconds_and_zones():
    assert parse_modtime("2026-11-05T12:00:00.123456789+09:00").isoformat() == "2026-11-05T03:00:00.123456+00:00"
    assert parse_modtime("2026-11-05T03:00:00Z").isoformat() == "2026-11-05T03:00:00+00:00"


def test_parse_entry_lowercases_hashes():
    entry = parse_entry({"Path": "a", "Size": 3, "ModTime": "2026-01-01T00:00:00Z", "Hashes": {"MD5": "x"}})
    assert entry.hashes == {"md5": "x"}


def test_acl_capture():
    meta = {"permissions": json.dumps([{"grantedToV2": {"user": {"email": "a@x"}}, "roles": ["read"]}])}
    assert acl_from_metadata(meta) == [{"principal": "a@x", "type": "user", "permission": "read"}]
    assert acl_from_metadata({}) is None and acl_from_metadata({"permissions": "not json"}) is None


def test_rrf_rewards_appearing_in_both_rankings():
    scores = rrf_merge([["a", "b"], ["b", "c"]], k=60)
    assert scores["b"] > scores["a"] > scores["c"]
    assert rrf_merge([["a"], ["a"]], k=60)["a"] == pytest.approx(1.0)


def _row(ordinal, score, heading="H", file_id="f"):
    return {"chunk_id": f"{file_id}{ordinal}", "file_id": file_id, "ordinal": ordinal, "heading_path": heading,
            "text": f"t{ordinal}", "page_start": ordinal, "page_end": ordinal, "quality": "ok", "score": score,
            "anchor": f"page:{ordinal}"}


def test_grouping_merges_neighbours_in_one_section():
    grouped = group_adjacent([_row(2, 0.9), _row(1, 0.8), _row(9, 0.7), _row(3, 0.6, heading="Other")])
    assert [g["chunk_ids"] for g in grouped] == [["f1", "f2"], ["f9"], ["f3"]]
    assert grouped[0]["text"] == "t1\n\nt2" and grouped[0]["score"] == 0.9
    assert (grouped[0]["page_start"], grouped[0]["page_end"]) == (1, 2)


def _record(level=logging.INFO, **extra):
    record = logging.LogRecord("doclib", level, "x", 1, "evt", (), None)
    record.__dict__.update(extra)
    return record


def test_logs_never_carry_document_content():
    secret = "the runbook password is hunter2"
    record = _record(text=secret, query=secret, chunks=3)
    DocumentContentFilter().filter(record)
    line = JsonFormatter("svc").format(record)
    assert "hunter2" not in line and json.loads(line)["chunks"] == 3


def test_debug_logs_trim_content_to_a_snippet():
    record = _record(level=logging.DEBUG, text="x" * 500)
    DocumentContentFilter().filter(record)
    assert len(record.__dict__["text"]) == 200


def test_log_lines_are_json_with_context():
    from doclib.log import bind, unbind
    token = bind(job_id=7, file_id="f")
    entry = json.loads(JsonFormatter("svc").format(_record()))
    unbind(token)
    assert entry["job_id"] == 7 and entry["file_id"] == "f" and entry["event"] == "evt" and entry["service"] == "svc"


def _change(change="new", outcome="indexed"):
    return {"change": change, "outcome": outcome, "file_id": "f", "path": "p", "previous_path": None,
            "content_version": "v", "error": None}


def test_digest_summary_and_text():
    changes = [_change(), _change("changed", "failed"), _change("deleted", "deleted")]
    summary = summarise(changes)
    assert summary == {"new": 1, "changed": 1, "deleted": 1, "moved": 0, "indexed": 1, "failed": 1, "skipped": 0}
    assert summary_text("ops", summary) == "ops: 1 new, 1 changed, 1 deleted, 1 failed"


def test_large_digests_split_into_parts_with_unique_ids():
    messages = build_payloads(tenant_id="t", source_id="s", scan_id="x", changes=[_change()] * 5, max_changes=2)
    payloads = [p for _, p in messages]
    assert [(p["part"], p["parts"], len(p["changes"])) for p in payloads] == [(1, 3, 2), (2, 3, 2), (3, 3, 1)]
    assert len({p["event_id"] for p in payloads}) == 3
    assert payloads[0]["schema_version"] == "1.0" and payloads[0]["event_type"] == "doclib.source.changes"


def test_webhook_signature_is_reproducible():
    assert sign("k", "1", b"{}") == sign("k", "1", b"{}") != sign("k", "2", b"{}")
    assert sign("k", "1", b"{}").startswith("sha256=")


def test_auth_none_uses_configured_tenant():
    identity = Authenticator(AuthConfig(mode="none"), "acme").identify({})
    assert identity.tenant_id == "acme"


def test_auth_header_requires_tenant():
    auth = Authenticator(AuthConfig(mode="header"), "acme")
    assert auth.identify({"x-tenant-id": "t1"}).tenant_id == "t1"
    with pytest.raises(AuthError):
        auth.identify({})


def test_auth_bearer(tmp_path):
    secret = "s" * 40
    path = tmp_path / "secret"
    path.write_text(secret)
    auth = Authenticator(AuthConfig(mode="bearer", token_secret_file=str(path)), "acme")
    good = jwt.encode({"tenant_id": "t9", "sub": "triage", "exp": time.time() + 60}, secret, algorithm="HS256")
    identity = auth.identify({"authorization": f"Bearer {good}"})
    assert (identity.tenant_id, identity.caller) == ("t9", "triage")
    forged = jwt.encode({"tenant_id": "t9", "exp": time.time() + 60}, "x" * 40, algorithm="HS256")
    expired = jwt.encode({"tenant_id": "t9", "exp": time.time() - 60}, secret, algorithm="HS256")
    no_tenant = jwt.encode({"exp": time.time() + 60}, secret, algorithm="HS256")
    for bad in (forged, expired, no_tenant, "garbage"):
        with pytest.raises(AuthError):
            auth.identify({"authorization": f"Bearer {bad}"})
    with pytest.raises(AuthError):
        auth.identify({})


def test_durations():
    assert parse_duration("15m") == 900 and parse_duration("90s") == 90 and parse_duration(5) == 5
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_config_env_overrides(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("embeddings: {endpoint: 'http://e/v1'}\nworker: {concurrency: 2}\n")
    settings = load_settings(str(path), {"DOCLIB_WORKER__CONCURRENCY": "7", "DOCLIB_TENANT_ID": "ignored-no-separator"})
    assert isinstance(settings, Settings) and settings.worker.concurrency == 7


def test_example_config_is_valid():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    settings = load_settings(os.path.join(root, "config.example.yaml"), {})
    assert settings.sources[0].scan_interval == 900 and settings.extraction.text.backend == "xberg"


async def test_dropped_connection_is_not_treated_as_an_offline_endpoint():
    """A page that crashes the server must use up attempts, not wait forever as if the endpoint were just down."""
    import httpx

    from doclib.config import VlmConfig
    from doclib.vlm import VlmClient, VlmConnectionLost, VlmUnavailable

    client = VlmClient(VlmConfig(enabled=True, endpoint="http://v/v1", retries=0))

    def drop(request):
        raise httpx.RemoteProtocolError("server disconnected")

    client._client = httpx.AsyncClient(base_url="http://v/v1", transport=httpx.MockTransport(drop))
    with pytest.raises(VlmConnectionLost):
        await client.page_to_markdown(b"x")
    assert not issubclass(VlmConnectionLost, VlmUnavailable)

    def refuse(request):
        raise httpx.ConnectError("refused")

    client._client = httpx.AsyncClient(base_url="http://v/v1", transport=httpx.MockTransport(refuse))
    with pytest.raises(VlmUnavailable):
        await client.page_to_markdown(b"x")


def test_licence_gate_logic():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[2] / "scripts" / "licence_report.py"
    spec = importlib.util.spec_from_file_location("licence_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    verdict = module.verdict
    assert verdict("MIT") == "ok"
    assert verdict("Apache-2.0 OR BSD-3-Clause") == "ok"
    assert verdict("BSL-1.0") == "review"  # Boost, which is permissive.
    assert verdict("BUSL-1.1") == "BLOCKED"  # Business Source, which is banned.
    assert verdict("AGPL-3.0-only") == "BLOCKED"
    assert verdict("MIT AND GPL-3.0") == "BLOCKED"  # AND: every part must pass.
    assert verdict("GPL-3.0 OR MIT") == "ok"  # OR: the best option applies.
    assert verdict("Apache-2.0 AND BSL-1.0") == "review"
    assert verdict("Proprietary") == "UNKNOWN"


def test_merged_results_cite_all_their_pages():
    rows = [{**_row(2, 0.9), "anchor": "page:2"}, {**_row(3, 0.8), "anchor": "page:3"}]
    merged, = group_adjacent(rows)
    assert merged["anchor"] == "page:2-3" and (merged["page_start"], merged["page_end"]) == (2, 3)
    headed = [{**_row(2, 0.9), "anchor": "heading:4.2", "page_start": None, "page_end": None},
              {**_row(3, 0.8), "anchor": "heading:4.2", "page_start": None, "page_end": None}]
    assert group_adjacent(headed)[0]["anchor"] == "heading:4.2"
