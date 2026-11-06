"""The connector API end to end against a real SeaweedFS over S3: create by API call, index, search, change, remove."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from doclib.admin_api import create_admin_app
from doclib.api import create_app
from doclib.scanner import reconcile, scan_source
from doclib.sources import load_enabled_sources, load_source
from doclib.worker import drain
from tests.fakes.fixtures import make_guide_pdf
from tests.integration.conftest import build_settings, default_source

ENDPOINT = os.environ.get("DOCLIB_TEST_S3_ENDPOINT")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not ENDPOINT, reason="no SeaweedFS container")]
BUCKET = "team-docs"


def s3(*args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """Test-only writes to the bucket. Uses its own rclone remote, outside doclib's read-only wrapper."""
    env = {**os.environ, "RCLONE_CONFIG_TESTS3_TYPE": "s3", "RCLONE_CONFIG_TESTS3_PROVIDER": "SeaweedFS",
           "RCLONE_CONFIG_TESTS3_ENDPOINT": ENDPOINT, "RCLONE_CONFIG_TESTS3_ACCESS_KEY_ID": os.environ["DOCLIB_TEST_S3_ACCESS_KEY"],
           "RCLONE_CONFIG_TESTS3_SECRET_ACCESS_KEY": os.environ["DOCLIB_TEST_S3_SECRET_KEY"]}
    return subprocess.run(["rclone", *args], env=env, capture_output=True, input=stdin, timeout=60)


def spec(**overrides) -> dict:
    return {"type": "s3", "root_path": f"{BUCKET}/ops",
            "options": {"provider": "SeaweedFS", "endpoint": ENDPOINT},
            "credentials": {"access_key_id": "DOCLIB_CRED_S3_ACCESS", "secret_access_key": "DOCLIB_CRED_S3_SECRET"},
            "scan_interval": "5m", **overrides}


@pytest.fixture
def bucket(tmp_path: Path):
    s3("purge", f"tests3:{BUCKET}")
    assert s3("mkdir", f"tests3:{BUCKET}").returncode == 0
    make_guide_pdf(tmp_path / "Guide.pdf")
    assert s3("copyto", str(tmp_path / "Guide.pdf"), f"tests3:{BUCKET}/ops/Vendors/Guide.pdf").returncode == 0
    assert s3("rcat", f"tests3:{BUCKET}/ops/notes.md", stdin=b"# Notes\n\nPage the team for quokka alarms.\n").returncode == 0
    yield
    s3("purge", f"tests3:{BUCKET}")


@pytest.fixture
def admin(settings, pool):
    with TestClient(create_admin_app(settings)) as client:
        yield client


async def index_connector(pool, settings, rclone, embedder, connector_id: str):
    source = await load_source(pool, connector_id)
    changes = await scan_source(pool, settings, rclone, source)
    await drain(pool, settings, rclone, embedder)
    return changes


def test_catalogue_endpoint_lists_rclone_backends(admin):
    types = {t["type"] for t in admin.get("/v1/connector-types").json()["types"]}
    assert {"s3", "smb", "webdav", "sftp"} <= types and "local" not in types
    detail = admin.get("/v1/connector-types/s3").json()
    assert any(o["name"] == "endpoint" for o in detail["options"])
    assert admin.get("/v1/connector-types/local").status_code == 404
    assert "/v1/connectors" in admin.get("/openapi.json").json()["paths"]


async def test_seaweedfs_from_api_call_to_search(admin, bucket, pool, settings, rclone, embedder, library):
    # One declarative call, and the connector exists. The connection is checked first.
    response = admin.put("/v1/connectors/seaweed-ops", json=spec(tags=["vendor:acme"]))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == "seaweed-ops" and body["origin"] == "api" and body["spec"]["type"] == "s3"
    assert body["spec"]["credentials"]["secret_access_key"] == "DOCLIB_CRED_S3_SECRET"
    assert "testsecret" not in response.text

    # Every process reads the registry, so the scanner picks it up with no restart.
    assert "seaweed-ops" in {s.id for s in await load_enabled_sources(pool)}
    assert await pool.fetchval("select scan_requested_at is not null from doclib.sources where source_id = 'seaweed-ops'")

    changes = await index_connector(pool, settings, rclone, embedder, "seaweed-ops")
    assert sorted(e.path for e in changes.new) == ["Vendors/Guide.pdf", "notes.md"]
    response = await library.search_docs("acme", "quokka alarms", filters=_only("seaweed-ops"))
    hit = response["results"][0]
    assert response["degraded"] == [] and hit["citation"]["path"] == "notes.md" and hit["citation"]["source_id"] == "seaweed-ops"
    assert (await pool.fetchval("select tags from doclib.files where path = 'notes.md' and source_id = 'seaweed-ops'")) == ["vendor:acme"]

    # Live reads go through the same connector.
    opened = await library.open_doc("acme", uri=hit["citation"]["uri"])
    assert "quokka" in opened["markdown"] and opened["stale_index"] is False
    pdf = (await library.search_docs("acme", "restarting SMF pods", filters=_only("seaweed-ops")))["results"][0]
    assert pdf["citation"]["anchor"].startswith("page:")
    assert "Restart SMF pods" in (await library.open_doc("acme", uri=pdf["citation"]["uri"]))["markdown"]

    # S3 gives real hashes, so renames are detected without re-extracting.
    assert s3("moveto", f"tests3:{BUCKET}/ops/notes.md", f"tests3:{BUCKET}/ops/renamed.md").returncode == 0
    changes = await index_connector(pool, settings, rclone, embedder, "seaweed-ops")
    assert [e.path for _, e in changes.moved] == ["renamed.md"], (changes.new, changes.deleted, changes.moved)

    # Status comes back in the same shape other services read.
    status = admin.get("/v1/connectors/seaweed-ops").json()["status"]
    assert status["files_by_status"] == {"indexed": 2} and status["last_scan_ok"] is True

    # Removing it removes the index, and leaves the bucket alone.
    removed = admin.delete("/v1/connectors/seaweed-ops")
    assert removed.status_code == 200 and removed.json()["files_removed"] == 2
    assert not (await library.search_docs("acme", "quokka alarms", filters=_only("seaweed-ops")))["results"]
    assert admin.get("/v1/connectors/seaweed-ops").status_code == 404
    assert "renamed.md" in s3("lsf", f"tests3:{BUCKET}/ops").stdout.decode()


def _only(source_id):
    from doclib.library import Filters
    return Filters(source_ids=[source_id])


def test_put_is_idempotent_and_post_refuses_duplicates(admin, bucket):
    assert admin.put("/v1/connectors/seaweed-ops", json=spec()).status_code == 201
    first = admin.get("/v1/connectors/seaweed-ops").json()
    assert admin.put("/v1/connectors/seaweed-ops", json=spec()).status_code == 200
    assert admin.get("/v1/connectors/seaweed-ops").json()["updated_at"] == first["updated_at"]  # No change, no rescan.
    assert admin.put("/v1/connectors/seaweed-ops", json=spec(tags=["new"])).json()["spec"]["tags"] == ["new"]
    duplicate = admin.post("/v1/connectors", json={"id": "seaweed-ops", **spec()})
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "already_exists"
    assert admin.post("/v1/connectors", json={"id": "second-one", **spec()}).status_code == 201
    assert {c["id"] for c in admin.get("/v1/connectors").json()["connectors"]} >= {"seaweed-ops", "second-one"}


def test_errors_are_well_defined_and_nothing_is_stored(admin, bucket, pool):
    def error_of(response, status):
        assert response.status_code == status, response.text
        return response.json()["error"]

    assert error_of(admin.put("/v1/connectors/bad", json=spec(type="local")), 422)["code"] == "unsupported_type"
    assert "must start with" in str(error_of(admin.put("/v1/connectors/bad-2", json=spec(
        credentials={"secret_access_key": "DOCLIB_POSTGRES__DSN"})), 422)["details"])
    assert error_of(admin.put("/v1/connectors/bad-3", json=spec(typo=1)), 422)["code"] == "invalid_request"
    wrong_port = spec(options={"provider": "SeaweedFS", "endpoint": "http://127.0.0.1:9"})
    failed = error_of(admin.put("/v1/connectors/bad-4?test=true", json=wrong_port), 422)
    assert failed["code"] == "connection_failed" and failed["details"]
    assert "testsecret" not in str(failed)
    assert error_of(admin.get("/v1/connectors/none-here"), 404)["code"] == "not_found"
    assert error_of(admin.delete("/v1/connectors/none-here"), 404)["code"] == "not_found"
    names = {c["id"] for c in admin.get("/v1/connectors").json()["connectors"]}
    assert not names & {"bad", "bad-2", "bad-3", "bad-4"}


def test_validate_checks_without_storing(admin, bucket):
    ok = admin.post("/v1/connectors/validate", json={"id": "dry-run", **spec()}).json()
    assert ok == {"valid": True, "connection": {"ok": True, "message": None}}
    bad_key = admin.post("/v1/connectors/validate", json={"id": "dry-run", **spec(
        credentials={"access_key_id": "DOCLIB_CRED_S3_SECRET", "secret_access_key": "DOCLIB_CRED_S3_ACCESS"})}).json()
    assert bad_key["connection"]["ok"] is False
    assert admin.get("/v1/connectors/dry-run").status_code == 404


def test_scan_now_and_config_file_sources_are_protected(admin, bucket, pool):
    assert admin.put("/v1/connectors/seaweed-ops", json=spec()).status_code == 201
    assert admin.post("/v1/connectors/seaweed-ops/scan").status_code == 202
    # "ops" comes from the config file, so the API must not change or delete it.
    assert admin.delete("/v1/connectors/ops").json()["error"]["code"] == "managed_by_config"
    assert admin.put("/v1/connectors/ops", json=spec()).json()["error"]["code"] == "already_exists"


async def test_scanner_starts_and_stops_loops_without_restart(admin, bucket, pool, settings, rclone):
    running: dict = {}
    try:
        await reconcile(pool, settings, rclone, running)
        assert set(running) == {"ops"}
        assert admin.put("/v1/connectors/seaweed-ops", json=spec()).status_code == 201
        await reconcile(pool, settings, rclone, running)
        assert set(running) == {"ops", "seaweed-ops"}
        task = running["seaweed-ops"][1]
        assert admin.put("/v1/connectors/seaweed-ops", json=spec(tags=["changed"])).status_code == 200
        await reconcile(pool, settings, rclone, running)
        assert running["seaweed-ops"][1] is not task  # New definition, new loop.
        assert admin.delete("/v1/connectors/seaweed-ops").status_code == 200
        await reconcile(pool, settings, rclone, running)
        assert set(running) == {"ops"}
    finally:
        for _, task in running.values():
            task.cancel()
        await asyncio.gather(*(t for _, t in running.values()), return_exceptions=True)


def test_agents_cannot_reach_connector_management(settings, pool):
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/connectors").status_code in {404, 405}
        assert client.put("/v1/connectors/x", json=spec()).status_code in {404, 405}
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        tools = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert not [t for t in tools.json()["result"]["tools"] if "connector" in t["name"]]


def test_admin_requires_scope_in_bearer_mode(share, embed_server, pool, tmp_path):
    secret_file = tmp_path / "secret"
    secret_file.write_text("s" * 40)
    secured = build_settings(embed_server.url, default_source(share),
                             admin={"auth": {"mode": "bearer", "token_secret_file": str(secret_file)}})

    def token(**claims):
        return {"Authorization": "Bearer " + jwt.encode({"tenant_id": "acme", "exp": time.time() + 60, **claims}, "s" * 40, algorithm="HS256")}

    with TestClient(create_admin_app(secured)) as client:
        assert client.get("/v1/connectors").status_code == 401
        assert client.get("/v1/connectors", headers=token()).status_code == 403
        assert client.get("/v1/connectors", headers=token(scope="doclib:admin")).status_code == 200
        assert client.get("/healthz").status_code == 200


def test_connectors_are_isolated_by_tenant(share, embed_server, pool, tmp_path, admin, bucket):
    secret_file = tmp_path / "secret"
    secret_file.write_text("s" * 40)
    secured = build_settings(embed_server.url, default_source(share),
                             admin={"auth": {"mode": "bearer", "token_secret_file": str(secret_file)}})
    other = {"Authorization": "Bearer " + jwt.encode(
        {"tenant_id": "other", "scope": "doclib:admin", "exp": time.time() + 60}, "s" * 40, algorithm="HS256")}
    assert admin.put("/v1/connectors/seaweed-ops", json=spec()).status_code == 201
    with TestClient(create_admin_app(secured)) as client:
        assert client.get("/v1/connectors/seaweed-ops", headers=other).status_code == 404
        assert client.delete("/v1/connectors/seaweed-ops", headers=other).status_code == 404
        assert client.get("/v1/connectors", headers=other).json()["connectors"] == []
        taken = client.put("/v1/connectors/seaweed-ops", headers=other, json=spec())
        assert taken.status_code == 409 and "acme" not in taken.text


async def test_bad_credentials_surface_on_the_status(admin, bucket, pool, settings, rclone, monkeypatch):
    assert admin.put("/v1/connectors/seaweed-ops", json=spec()).status_code == 201
    monkeypatch.delenv("DOCLIB_CRED_S3_SECRET")
    source = await load_source(pool, "seaweed-ops")
    with pytest.raises(Exception):  # noqa: B017
        await scan_source(pool, settings, rclone, source)
    status = admin.get("/v1/connectors/seaweed-ops").json()["status"]
    assert status["last_scan_ok"] is False and "DOCLIB_CRED_S3_SECRET" in status["last_scan_error"]


# The admin MCP tools, over the real ASGI app and a real SeaweedFS.

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
SETUP_TOOLS = {"list_connection_profiles", "list_connectors", "get_connector", "validate_connector", "create_connector", "scan_connector"}


@pytest.fixture
def mcp_admin(share, embed_server, pool, tmp_path):
    secret_file = tmp_path / "secret"
    secret_file.write_text("s" * 40)
    profile = {key: spec()[key] for key in ("type", "options", "credentials")}
    secured = build_settings(embed_server.url, default_source(share), admin={
        "auth": {"mode": "bearer", "token_secret_file": str(secret_file)},
        "mcp": {"enabled": True, "profiles": {"team": {**profile, "description": "Team bucket", "allowed_roots": [f"{BUCKET}/ops"]}}}})
    with TestClient(create_admin_app(secured)) as client:
        yield client


def bearer(**claims) -> dict:
    token = jwt.encode({"tenant_id": "acme", "sub": "setup-agent", "exp": time.time() + 60, **claims}, "s" * 40, algorithm="HS256")
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}"}


def rpc(client, method: str, params: dict | None = None, headers: dict | None = None):
    return client.post("/mcp", headers=headers or bearer(scope="doclib:admin"),
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def tool(client, name: str, **arguments) -> dict:
    reply = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    assert reply.status_code == 200, reply.text
    return json.loads(reply.json()["result"]["content"][0]["text"])


def connector_ids(client) -> set[str]:
    return {c["id"] for c in tool(client, "list_connectors")["connectors"]}


def test_admin_mcp_is_absent_unless_turned_on(admin):
    assert admin.post("/mcp", headers=MCP_HEADERS, json={}).status_code in {404, 405}


def test_admin_mcp_needs_a_token_with_the_admin_scope(mcp_admin):
    assert rpc(mcp_admin, "tools/list", headers=MCP_HEADERS).status_code == 401
    assert rpc(mcp_admin, "tools/list", headers=bearer()).status_code == 403
    assert rpc(mcp_admin, "tools/list", headers=bearer(scope="doclib:read")).status_code == 403
    assert rpc(mcp_admin, "tools/list").status_code == 200


def test_admin_mcp_can_add_but_not_change_or_remove(mcp_admin):
    tools = rpc(mcp_admin, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == SETUP_TOOLS
    create = next(t for t in tools if t["name"] == "create_connector")
    assert set(create["inputSchema"]["properties"]) == {"connector_id", "profile", "root_path", "include", "exclude", "tags"}


def test_profiles_are_listed_without_their_destination_or_credentials(mcp_admin):
    assert tool(mcp_admin, "list_connection_profiles") == {"profiles": [
        {"name": "team", "description": "Team bucket", "type": "s3", "allowed_roots": [f"{BUCKET}/ops"]}]}


async def test_agent_adds_a_source_over_mcp_and_it_becomes_searchable(mcp_admin, pool, settings, rclone, embedder, library, bucket):
    assert tool(mcp_admin, "validate_connector", connector_id="team-ops", profile="team", root_path=f"{BUCKET}/ops") == {
        "valid": True, "connection": {"ok": True, "message": None}}
    assert connector_ids(mcp_admin) == {"ops"}  # Validating stores nothing. "ops" is the test config's own source.

    created = tool(mcp_admin, "create_connector", connector_id="team-ops", profile="team", root_path=f"{BUCKET}/ops", tags=["team:ops"])
    assert created["id"] == "team-ops" and created["spec"]["options"]["endpoint"] == ENDPOINT and created["spec"]["tags"] == ["team:ops"]
    assert tool(mcp_admin, "get_connector", connector_id="team-ops")["id"] == "team-ops"
    assert tool(mcp_admin, "scan_connector", connector_id="team-ops") == {"scan_requested": True}

    await index_connector(pool, settings, rclone, embedder, "team-ops")
    results = (await library.search_docs("acme", "quokka alarms", filters=_only("team-ops")))["results"]
    assert results[0]["citation"]["path"] == "notes.md"


def test_agent_cannot_pick_its_own_destination(mcp_admin, bucket):
    created = tool(mcp_admin, "create_connector", connector_id="team-ops", profile="team", root_path=f"{BUCKET}/ops",
                   type="sftp", options={"endpoint": "http://attacker.example"}, endpoint="http://attacker.example",
                   credentials={"access_key_id": "DOCLIB_CRED_S3_SECRET"})
    assert created["spec"]["type"] == "s3" and created["spec"]["options"]["endpoint"] == ENDPOINT
    assert created["spec"]["credentials"]["access_key_id"] == "DOCLIB_CRED_S3_ACCESS"


def test_agent_is_refused_outside_its_profile(mcp_admin, bucket):
    for root in (BUCKET, f"{BUCKET}/hr", f"{BUCKET}/ops/../hr"):
        reply = tool(mcp_admin, "create_connector", connector_id="team-hr", profile="team", root_path=root)
        assert reply["error"]["code"] == "root_not_allowed"
    assert tool(mcp_admin, "validate_connector", connector_id="team-hr", profile="team", root_path=f"{BUCKET}/hr")["error"]["code"] == "root_not_allowed"
    assert tool(mcp_admin, "create_connector", connector_id="x-y", profile="nope", root_path="a")["error"]["code"] == "unknown_profile"
    assert connector_ids(mcp_admin) == {"ops"}


def test_agent_cannot_overwrite_an_existing_connector(mcp_admin, bucket):
    other = spec(root_path=f"{BUCKET}/ops/Vendors", tags=["owner:platform"])
    assert mcp_admin.put("/v1/connectors/platform-docs", json=other, headers=bearer(scope="doclib:admin")).status_code == 201
    reply = tool(mcp_admin, "create_connector", connector_id="platform-docs", profile="team", root_path=f"{BUCKET}/ops")
    assert reply["error"]["code"] == "already_exists"
    assert mcp_admin.get("/v1/connectors/platform-docs", headers=bearer(scope="doclib:admin")).json()["spec"]["tags"] == ["owner:platform"]


def test_admin_mcp_tenant_comes_from_the_token(mcp_admin, bucket):
    tool(mcp_admin, "create_connector", connector_id="team-ops", profile="team", root_path=f"{BUCKET}/ops")
    other = rpc(mcp_admin, "tools/call", {"name": "list_connectors", "arguments": {}}, headers=bearer(scope="doclib:admin", tenant_id="other"))
    assert json.loads(other.json()["result"]["content"][0]["text"]) == {"connectors": []}


def test_creating_connectors_works_with_info_logging(mcp_admin, bucket):
    # Other tests log at WARNING. This once failed only at INFO, after the connector had been stored.
    logger = logging.getLogger("doclib")
    logger.setLevel(logging.INFO)
    try:
        assert "id" in tool(mcp_admin, "create_connector", connector_id="team-ops", profile="team", root_path=f"{BUCKET}/ops")
        reply = mcp_admin.put("/v1/connectors/by-rest", json=spec(), headers=bearer(scope="doclib:admin"))
        assert reply.status_code == 201, reply.text
    finally:
        logger.setLevel(logging.NOTSET)


async def test_portal_adds_scans_and_deletes_a_source(admin, bucket, pool, settings, rclone, embedder):
    headers = {"X-Portal": "1"}
    body = {"id": "from-portal", **spec()}
    assert admin.post("/portal/api/connectors/test", json=body, headers=headers).json() == {"ok": True, "message": None}
    assert admin.post("/portal/api/connectors", json=body, headers=headers).status_code == 201
    assert admin.post("/portal/api/connectors", json=body, headers=headers).status_code == 409
    await index_connector(pool, settings, rclone, embedder, "from-portal")
    listed = {c["id"]: c for c in admin.get("/portal/api/connectors").json()["connectors"]}
    assert listed["from-portal"]["status"]["files_by_status"]["indexed"] == 2
    assert admin.post("/portal/api/connectors/from-portal/scan", headers=headers).status_code == 202
    assert admin.delete("/portal/api/connectors/from-portal", headers=headers).json()["files_removed"] == 2

