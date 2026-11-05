"""Connector validation against rclone's real catalogue. No database or network needed."""

import pytest

from doclib.config import ConnectorPolicy
from doclib.connectors import ConnectorError, ConnectorService, ConnectorSpec, backend_detail
from doclib.rclone import Rclone, WriteAttemptError, remote_name

SEAWEED = {"type": "s3", "root_path": "docs/ops",
           "options": {"provider": "SeaweedFS", "endpoint": "http://seaweed:8333"},
           "credentials": {"access_key_id": "DOCLIB_CRED_ACCESS", "secret_access_key": "DOCLIB_CRED_SECRET"}}


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setenv("DOCLIB_CRED_ACCESS", "AKIAEXAMPLE")
    monkeypatch.setenv("DOCLIB_CRED_SECRET", "sup3rs3cret")
    monkeypatch.setenv("DOCLIB_POSTGRES_PASSWORD", "dbpass")
    return ConnectorService(None, Rclone(), ConnectorPolicy())


async def problems(service, **overrides):
    with pytest.raises(ConnectorError) as caught:
        await service.validate("my-source", ConnectorSpec(**{**SEAWEED, **overrides}), "t")
    return caught.value


async def test_seaweedfs_definition_is_valid(service):
    source = await service.validate("seaweed-ops", ConnectorSpec(**SEAWEED), "acme")
    assert source.type == "s3" and source.tenant_id == "acme" and source.rclone_remote is None
    assert Rclone.target(source) == "dl_seaweed_ops:docs/ops"


async def test_catalogue_comes_from_rclone_and_flags_secrets(service):
    catalogue = await service.catalogue()
    assert {"s3", "smb", "webdav", "sftp", "onedrive"} <= set(catalogue)
    options = {o["name"]: o for o in backend_detail(catalogue["s3"])["options"]}
    assert options["secret_access_key"]["secret"] is True
    assert options["endpoint"]["secret"] is False
    assert "SeaweedFS" in options["provider"]["examples"]


@pytest.mark.parametrize("backend", ["local", "alias", "crypt", "union", "combine", "chunker", "memory", "http", "nonsense"])
async def test_dangerous_or_unknown_types_are_refused(service, backend):
    error = await problems(service, type=backend, options={}, credentials={})
    assert error.code == "unsupported_type"


async def test_unknown_option_is_refused(service):
    error = await problems(service, options={**SEAWEED["options"], "endpiont": "typo"})
    assert error.code == "invalid_connector" and "endpiont" in str(error.details)


async def test_secrets_in_plain_options_are_refused(service):
    error = await problems(service, options={**SEAWEED["options"], "secret_access_key": "oops"})
    assert "through credentials" in str(error.details)


async def test_credentials_cannot_read_arbitrary_environment_variables(service):
    """Otherwise a caller could point endpoint at their own server and steal this service's secrets."""
    error = await problems(service, credentials={"secret_access_key": "DOCLIB_POSTGRES_PASSWORD"})
    assert "must start with DOCLIB_CRED_" in str(error.details)


async def test_missing_environment_variable_is_reported_by_name(service):
    error = await problems(service, credentials={"secret_access_key": "DOCLIB_CRED_NOT_SET"})
    assert "DOCLIB_CRED_NOT_SET is not set" in str(error.details)


async def test_required_options_are_enforced(service):
    error = await problems(service, type="smb", options={}, credentials={})
    assert "host: required" in str(error.details)


@pytest.mark.parametrize("bad", ["UPPER", "has_underscore", "-leading", "x", "a/b", "semi;colon", "a" * 64])
async def test_bad_ids_are_refused(service, bad):
    with pytest.raises(ConnectorError) as caught:
        await service.validate(bad, ConnectorSpec(**SEAWEED), "t")
    assert caught.value.code == "invalid_id"


def test_spec_rejects_typos_and_traversal():
    with pytest.raises(ValueError):
        ConnectorSpec(**SEAWEED, scan_intervall="5m")
    with pytest.raises(ValueError):
        ConnectorSpec(**{**SEAWEED, "root_path": "docs/../../etc"})
    with pytest.raises(ValueError):
        ConnectorSpec(**SEAWEED, scan_interval="1s")


async def test_secrets_travel_in_env_not_argv(service):
    source = await service.validate("seaweed-ops", ConnectorSpec(**SEAWEED), "acme")
    rclone = Rclone()
    env = await rclone.env_for(source)
    assert env["RCLONE_CONFIG_DL_SEAWEED_OPS_TYPE"] == "s3"
    assert env["RCLONE_CONFIG_DL_SEAWEED_OPS_PROVIDER"] == "SeaweedFS"
    assert env["RCLONE_CONFIG_DL_SEAWEED_OPS_SECRET_ACCESS_KEY"] == "sup3rs3cret"
    argv = rclone._argv("lsjson", [rclone.target(source)], source)
    assert "sup3rs3cret" not in " ".join(argv) and "AKIAEXAMPLE" not in " ".join(argv)


async def test_password_options_are_obscured_for_rclone(service, monkeypatch):
    monkeypatch.setenv("DOCLIB_CRED_SMB", "hunter2")
    source = await service.validate("smb-ops", ConnectorSpec(type="smb", root_path="ops", options={"host": "h"},
                                                              credentials={"pass": "DOCLIB_CRED_SMB"}), "t")
    env = await Rclone().env_for(source)
    value = env["RCLONE_CONFIG_DL_SMB_OPS_PASS"]
    assert value and value != "hunter2"  # rclone rejects plain text here.


async def test_stored_config_holds_names_not_values(service):
    source = await service.validate("seaweed-ops", ConnectorSpec(**SEAWEED), "acme")
    stored = source.model_dump_json()
    assert "DOCLIB_CRED_SECRET" in stored and "sup3rs3cret" not in stored


def test_write_config_is_still_blocked():
    rclone = Rclone()
    with pytest.raises(WriteAttemptError):
        rclone._argv("config", ["create", "x", "s3"], None)


def test_remote_name_is_env_safe():
    from doclib.config import SourceConfig
    source = SourceConfig(id="my-seaweed-2", type="s3", root_path="b")
    assert remote_name(source) == "dl_my_seaweed_2"


async def test_hostnames_and_usernames_are_not_treated_as_secrets(service):
    """rclone flags these Sensitive for log redaction, so a plain hostname must still be accepted."""
    source = await service.validate("smb-ops", ConnectorSpec(type="smb", root_path="ops", options={"host": "h", "user": "u"}), "t")
    assert source.options == {"host": "h", "user": "u"}


async def test_every_allowed_type_exists_in_rclone(service):
    assert not set(ConnectorPolicy().allowed_types) - set(await service.catalogue())
