"""The web portal over the real ASGI app, with a real database and fake webhook and embedding servers."""

from __future__ import annotations

import base64
import time
from importlib import resources

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from doclib import notify
from doclib.admin_api import create_admin_app
from doclib.config import WebhookConfig
from tests.integration.conftest import build_settings, default_source, resync

pytestmark = pytest.mark.integration
HEADERS = {"X-Portal": "1"}
CONFIG = WebhookConfig(digest_max_wait=300)


@pytest.fixture
def portal(settings, pool):
    with TestClient(create_admin_app(settings)) as client:
        yield client


@pytest.fixture
def hook(webhook_server):
    return f"{webhook_server.url}/hook"


def basic(user: str, password: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode(), **HEADERS}


def test_page_is_served_with_no_login_by_default(portal):
    page = portal.get("/")
    assert page.status_code == 200 and "<title>Document Library</title>" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert portal.get("/portal/api/status").status_code == 200


def test_login_is_required_once_a_password_is_set(settings, pool, monkeypatch):
    monkeypatch.setenv("DOCLIB_PORTAL_USERNAME", "ops")
    monkeypatch.setenv("DOCLIB_PORTAL_PASSWORD", "boot-password")
    with TestClient(create_admin_app(settings)) as client:
        for path in ("/", "/portal/api/status", "/portal/api/webhook"):
            refused = client.get(path)
            assert refused.status_code == 401 and refused.headers["www-authenticate"].startswith("Basic")
        assert client.get("/", headers=basic("ops", "wrong")).status_code == 401
        assert client.get("/", headers=basic("admin", "boot-password")).status_code == 401
        assert client.get("/", headers={"Authorization": "Basic !!!"}).status_code == 401
        assert client.get("/portal/api/status", headers=basic("ops", "boot-password")).status_code == 200


def test_actions_need_the_portal_header(portal):
    assert portal.post("/portal/api/webhook/retry").status_code == 403
    assert portal.post("/portal/api/webhook/retry", headers=HEADERS).status_code == 200


def test_portal_can_be_turned_off(share, embed_server, pool):
    off = build_settings(embed_server.url, default_source(share), admin={"portal": {"enabled": False}})
    with TestClient(create_admin_app(off)) as client:
        assert client.get("/").status_code == 404
        assert client.get("/portal/api/status").status_code == 404


def test_portal_works_while_the_rest_api_needs_tokens(share, embed_server, pool, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("s" * 40)
    secured = build_settings(embed_server.url, default_source(share),
                             admin={"auth": {"mode": "bearer", "token_secret_file": str(secret)}})
    with TestClient(create_admin_app(secured)) as client:
        assert client.get("/v1/connectors").status_code == 401
        assert client.get("/portal/api/connectors").status_code == 200
        token = jwt.encode({"tenant_id": "acme", "scope": "doclib:admin", "exp": time.time() + 60}, "s" * 40, algorithm="HS256")
        assert client.get("/v1/connectors", headers={"Authorization": f"Bearer {token}"}).status_code == 200


async def test_status_and_search_show_the_index(portal, indexed):
    status = portal.get("/portal/api/status").json()
    assert status["sources"][0]["source_id"] == "ops" and status["sources"][0]["files_by_status"]["indexed"] > 0
    assert status["webhook"] == {} and status["failed_files"] == 0
    found = portal.post("/portal/api/search", json={"query": "quokka alarms"}, headers=HEADERS).json()
    assert "quokka" in found["results"][0]["text"] and found["results"][0]["citation"]["path"]


def test_catalogue_shows_only_basic_options_with_required_first(portal):
    assert "local" not in {t["type"] for t in portal.get("/portal/api/connector-types").json()["types"]}
    detail = portal.get("/portal/api/connector-types/sftp").json()
    names = [o["name"] for o in detail["options"]]
    assert "host" in names and not any(o["advanced"] for o in detail["options"])
    required = [o["required"] for o in detail["options"]]
    assert required == sorted(required, reverse=True)
    assert portal.get("/portal/api/connector-types/local").status_code == 404


def test_connector_errors_come_back_readable(portal):
    bad = portal.post("/portal/api/connectors", headers=HEADERS, json={
        "id": "x-y", "type": "s3", "root_path": "b", "options": {"secret_access_key": "oops"}})
    assert bad.status_code == 422 and "secret" in " ".join(bad.json()["error"]["details"])
    assert portal.delete("/portal/api/connectors/ops", headers=HEADERS).status_code == 409  # From the config file.
    assert portal.post("/portal/api/connectors/nope/scan", headers=HEADERS).status_code == 404


def test_webhook_settings_are_checked(portal, monkeypatch):
    def save(**form):
        return portal.put("/portal/api/webhook", json=form, headers=HEADERS)

    assert save(enabled=True).status_code == 422
    assert save(enabled=True, url="ftp://x").status_code == 422
    assert save(enabled=True, url="https://").status_code == 422
    assert save(enabled=False, url="not a url").status_code == 422
    assert save(enabled=True, url="https://x.test", secret_env="DOCLIB_POSTGRES_PASSWORD").status_code == 422
    assert save(enabled=True, url="https://x.test", secret_env="DOCLIB_CRED_MISSING").status_code == 422
    monkeypatch.setenv("DOCLIB_CRED_HOOK", "shh")
    saved = save(enabled=True, url="https://x.test/a", secret_env="DOCLIB_CRED_HOOK")
    assert saved.status_code == 200
    body = saved.json()
    assert body["enabled"] and body["url"] == "https://x.test/a" and body["signed"] and body["origin"] == "portal"
    assert "shh" not in saved.text


async def test_saved_webhook_takes_effect_without_a_restart(portal, indexed, share, settings, rclone, embedder, hook, webhook_state):
    # Off by default, so nothing goes out.
    target = await notify.load_target(indexed, CONFIG)
    assert target.origin == "config" and not target.enabled

    assert portal.put("/portal/api/webhook", json={"enabled": True, "url": hook}, headers=HEADERS).status_code == 200
    (share / "added.md").write_text("# Added\n\nA new wombat page.\n")
    await resync(indexed, settings, rclone, embedder)
    target = await notify.load_target(indexed, CONFIG)
    assert target.url == hook and target.enabled and target.origin == "portal"
    async with httpx.AsyncClient() as client:
        await notify.poll_once(indexed, CONFIG, client, target)
    assert [m["json"]["summary"]["new"] for m in webhook_state["received"]] == [1]

    deliveries = portal.get("/portal/api/webhook").json()["deliveries"]
    assert deliveries[0]["status"] == "sent" and "1 new" in deliveries[0]["text"]

    assert portal.put("/portal/api/webhook", json={"enabled": False, "url": hook}, headers=HEADERS).status_code == 200
    (share / "more.md").write_text("# More\n\nAnother wombat page.\n")
    await resync(indexed, settings, rclone, embedder)
    target = await notify.load_target(indexed, CONFIG)
    async with httpx.AsyncClient() as client:
        await notify.poll_once(indexed, CONFIG, client, target)
    assert len(webhook_state["received"]) == 1


async def test_turning_the_webhook_on_does_not_send_the_backlog(portal, indexed, share, settings, rclone, embedder, hook, webhook_state):
    (share / "old.md").write_text("# Old\n\nChanged while the webhook was off.\n")
    await resync(indexed, settings, rclone, embedder)
    assert await indexed.fetchval("select count(*) from doclib.change_events where delivery_id is null") == 1
    portal.put("/portal/api/webhook", json={"enabled": True, "url": hook}, headers=HEADERS)
    target = await notify.load_target(indexed, CONFIG)
    async with httpx.AsyncClient() as client:
        await notify.poll_once(indexed, CONFIG, client, target)
    assert webhook_state["received"] == []


async def test_signing_secret_missing_holds_messages_rather_than_sending_unsigned(indexed, hook, monkeypatch, webhook_state):
    await indexed.execute("insert into doclib.settings (key, value) values ('webhook', $1)",
                          {"enabled": True, "url": hook, "secret_env": "DOCLIB_CRED_GONE"})
    await indexed.execute("insert into doclib.webhook_deliveries (delivery_id, tenant_id, source_id, payload) values "
                          "(gen_random_uuid(), 'acme', 'ops', $1)", {"text": "x"})
    target = await notify.load_target(indexed, CONFIG)
    assert "DOCLIB_CRED_GONE" in target.problem
    async with httpx.AsyncClient() as client:
        await notify.poll_once(indexed, CONFIG, client, target)
    assert webhook_state["received"] == []


def test_send_test_is_signed_and_reports_the_result(portal, hook, monkeypatch, webhook_state):
    monkeypatch.setenv("DOCLIB_CRED_HOOK", "shh")
    sent = portal.post("/portal/api/webhook/test", json={"url": hook, "secret_env": "DOCLIB_CRED_HOOK"}, headers=HEADERS)
    assert sent.json() == {"ok": True, "status": 200}
    message, = webhook_state["received"]
    assert message["json"]["event_type"] == "doclib.test"
    expected = notify.sign("shh", message["headers"]["x-doclib-timestamp"], message["body"])
    assert message["headers"]["x-doclib-signature"] == expected

    webhook_state["control"]["status"] = 500
    assert portal.post("/portal/api/webhook/test", json={"url": hook}, headers=HEADERS).json() == {"ok": False, "status": 500}
    down = portal.post("/portal/api/webhook/test", json={"url": "http://127.0.0.1:9/token-in-url"}, headers=HEADERS).json()
    assert down["ok"] is False and "token-in-url" not in str(down)
    assert portal.get("/portal/api/webhook").json()["deliveries"] == []  # Tests aren't change notifications.


async def test_retry_failed_and_new_url_resend_waiting_messages(portal, indexed, hook, webhook_state):
    for status in ("dead", "pending"):
        await indexed.execute(
            "insert into doclib.webhook_deliveries (delivery_id, tenant_id, source_id, payload, status, attempts, next_attempt_at) "
            "values (gen_random_uuid(), 'acme', 'ops', $1, $2, 5, now() + interval '1 hour')", {"text": status}, status)
    assert portal.post("/portal/api/webhook/retry", headers=HEADERS).json() == {"retried": 1}
    portal.put("/portal/api/webhook", json={"enabled": True, "url": hook}, headers=HEADERS)
    target = await notify.load_target(indexed, CONFIG)
    async with httpx.AsyncClient() as client:
        await notify.poll_once(indexed, CONFIG, client, target)
    assert sorted(m["json"]["text"] for m in webhook_state["received"]) == ["dead", "pending"]


def test_models_are_shown_but_cannot_be_changed(portal):
    data = portal.get("/portal/api/models").json()
    roles = {m["role"]: m for m in data["models"]}
    assert roles["Embedding"]["model"] == "fake" and roles["Embedding"]["state"] == "reachable"
    assert roles["Vision"]["state"] == "off" and roles["Text"]["model"] == "xberg"
    assert portal.put("/portal/api/models", json={}, headers=HEADERS).status_code in {404, 405}


def test_models_page_reports_an_unreachable_embedding_service(share, embed_server, pool):
    broken = build_settings(embed_server.url, default_source(share),
                            embeddings={"endpoint": "http://127.0.0.1:9/v1", "model": "fake", "dimension": 1024})
    with TestClient(create_admin_app(broken)) as client:
        states = {m["role"]: m["state"] for m in client.get("/portal/api/models").json()["models"]}
    assert states["Embedding"] == "unreachable"


def test_page_never_builds_html_from_strings():
    html = (resources.files("doclib") / "portal.html").read_text(encoding="utf-8")
    assert "innerHTML" not in html and "insertAdjacentHTML" not in html and "document.write" not in html
