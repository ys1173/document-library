"""The limits on what an agent can do through the admin MCP tools. No database or network needed."""

import pytest
from pydantic import ValidationError

from doclib.admin_mcp import profile_spec, root_allowed
from doclib.config import DEFAULT_EXCLUDE, AdminConfig, ConnectionProfile
from doclib.connectors import ConnectorError

PROFILE = ConnectionProfile(description="Team bucket", type="s3", options={"provider": "SeaweedFS", "endpoint": "http://seaweed:8333"},
                            credentials={"access_key_id": "DOCLIB_CRED_ACCESS"}, allowed_roots=["team-docs/ops", "shared/"],
                            scan_interval="1h")


@pytest.mark.parametrize("root", ["team-docs/ops", "team-docs/ops/", "team-docs/ops/vendors", "/team-docs/ops/a/b", "shared/x"])
def test_folders_inside_an_allowed_root_pass(root):
    assert root_allowed(root, PROFILE.allowed_roots)


@pytest.mark.parametrize("root", ["team-docs", "team-docs/operations", "team-docs/ops2/x", "other/ops", "", "/",
                                  "team-docs/ops/../hr", "team-docs/ops/./x", "team-docs//ops", "team-docs\\ops\\..\\hr"])
def test_folders_outside_an_allowed_root_are_refused(root):
    assert not root_allowed(root, PROFILE.allowed_roots)


def test_star_allows_the_store_but_not_traversal():
    assert root_allowed("anything/here", ["*"])
    assert not root_allowed("anything/../here", ["*"])
    assert not root_allowed("", ["*"])


def test_destination_and_credentials_come_only_from_the_profile():
    spec = profile_spec("team", PROFILE, "team-docs/ops/vendors", include=["**.pdf"], exclude=["**/Drafts/**"], tags=["team:ops"])
    assert spec.type == "s3" and spec.options == PROFILE.options and spec.credentials == PROFILE.credentials
    assert spec.root_path == "team-docs/ops/vendors" and spec.scan_interval == "1h"
    assert spec.include == ["**.pdf"] and spec.tags == ["team:ops"]


def test_agent_excludes_add_to_the_defaults_rather_than_replacing_them():
    assert profile_spec("team", PROFILE, "shared/x", exclude=["**/Drafts/**"]).exclude == [*DEFAULT_EXCLUDE, "**/Drafts/**"]
    assert profile_spec("team", PROFILE, "shared/x").exclude == list(DEFAULT_EXCLUDE)


def test_refused_folder_is_a_clear_error():
    with pytest.raises(ConnectorError) as caught:
        profile_spec("team", PROFILE, "team-docs/hr")
    assert caught.value.code == "root_not_allowed" and caught.value.details == {"allowed_roots": PROFILE.allowed_roots}


def test_profile_must_name_its_allowed_roots():
    with pytest.raises(ValidationError):
        ConnectionProfile(type="s3", allowed_roots=[])
    with pytest.raises(ValidationError):
        ConnectionProfile(type="s3")


def test_admin_mcp_is_off_by_default():
    assert AdminConfig().mcp.enabled is False


@pytest.mark.parametrize("mode", ["none", "header"])
def test_admin_mcp_refuses_to_start_without_tokens(mode):
    with pytest.raises(ValidationError, match="requires admin.auth.mode: bearer"):
        AdminConfig.model_validate({"auth": {"mode": mode}, "mcp": {"enabled": True}})


def test_admin_mcp_starts_with_tokens():
    config = AdminConfig.model_validate({"auth": {"mode": "bearer", "token_secret_file": "/x"}, "mcp": {"enabled": True}})
    assert config.mcp.enabled
