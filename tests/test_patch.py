from __future__ import annotations

import sys
import importlib

from requests import Request

from embucket_spcs_connector import patch


def _prepared_headers(auth, token="embucket-session-token"):
    request = Request("POST", "https://example.snowflakecomputing.app")
    prepared = request.prepare()
    auth_instance = auth(token)
    return auth_instance(prepared).headers


def test_login_request_uses_spcs_authorization_only():
    import snowflake.connector.network as network

    patch(spcs_token="spcs-token")

    headers = _prepared_headers(network.SnowflakeAuth, network.NO_TOKEN)

    assert headers["Authorization"] == 'Snowflake Token="spcs-token"'
    assert "X-Embucket-Authorization" not in headers


def test_login_request_treats_string_none_as_no_embucket_token():
    import snowflake.connector.network as network

    patch(spcs_token="spcs-token")

    headers = _prepared_headers(network.SnowflakeAuth, "None")

    assert headers["Authorization"] == 'Snowflake Token="spcs-token"'
    assert "X-Embucket-Authorization" not in headers


def test_query_request_uses_spcs_authorization_only():
    import snowflake.connector.network as network

    patch(spcs_token="spcs-token")

    headers = _prepared_headers(network.SnowflakeAuth, "embucket-session-token")

    assert headers["Authorization"] == 'Snowflake Token="spcs-token"'
    assert "X-Embucket-Authorization" not in headers


def test_spcs_token_file_defaults_next_to_config(monkeypatch, tmp_path):
    import snowflake.connector.network as network

    config_file = tmp_path / "config.toml"
    token_file = tmp_path / "embucket_spcs_token"
    config_file.write_text("[connections.embucket_spcs]\n", encoding="utf-8")
    token_file.write_text("spcs-token-from-file", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["embucket-snow", "--config-file", str(config_file), "sql"],
    )

    patch()

    headers = _prepared_headers(network.SnowflakeAuth, "embucket-session-token")

    assert headers["Authorization"] == 'Snowflake Token="spcs-token-from-file"'
    assert "X-Embucket-Authorization" not in headers


def test_spcs_token_file_is_re_read_for_rotation(tmp_path):
    import snowflake.connector.network as network

    token_file = tmp_path / "embucket_spcs_token"
    token_file.write_text("spcs-token-1", encoding="utf-8")

    patch(spcs_token_file=str(token_file))

    headers = _prepared_headers(network.SnowflakeAuth, "embucket-session-token")
    assert headers["Authorization"] == 'Snowflake Token="spcs-token-1"'

    token_file.write_text("spcs-token-2", encoding="utf-8")

    headers = _prepared_headers(network.SnowflakeAuth, "embucket-session-token")
    assert headers["Authorization"] == 'Snowflake Token="spcs-token-2"'


def test_spcs_token_command_can_supply_rotated_token(tmp_path):
    import snowflake.connector.network as network

    token_file = tmp_path / "command_token"
    token_file.write_text("spcs-token-from-command", encoding="utf-8")
    script = tmp_path / "token_command.py"
    script.write_text(
        "from pathlib import Path\n"
        f"print(Path({str(token_file)!r}).read_text().strip())\n",
        encoding="utf-8",
    )

    patch(spcs_token_command=f"{sys.executable} {script}")

    headers = _prepared_headers(network.SnowflakeAuth, "embucket-session-token")
    assert headers["Authorization"] == 'Snowflake Token="spcs-token-from-command"'

    token_file.write_text("spcs-token-from-command-2", encoding="utf-8")

    headers = _prepared_headers(network.SnowflakeAuth, "embucket-session-token")
    assert headers["Authorization"] == 'Snowflake Token="spcs-token-from-command-2"'


def test_spcs_token_can_be_issued_from_snowflake_cli_connection(monkeypatch, tmp_path):
    import snowflake.connector
    import snowflake.connector.network as network
    patch_module = importlib.import_module("embucket_spcs_connector.patch")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
default_connection_name = "embucket_spcs"

[connections.embucket_spcs]
host = "example.snowflakecomputing.app"
account = "embucket"
user = "embucket"
password = "embucket"
spcs_token_connection = "snowflake"

[connections.snowflake]
account = "example-account"
user = "real-user"
password = "real-password"
role = "ACCOUNTADMIN"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        ["embucket-snow", "--config-file", str(config_file), "sql", "-c", "embucket_spcs"],
    )

    captured_params = {}

    class FakeRest:
        def _token_request(self, request_type):
            assert request_type == "ISSUE"
            return {"data": {"sessionToken": "issued-spcs-token", "validityInSecondsST": 3600}}

    class FakeConnection:
        _rest = FakeRest()

        def close(self):
            pass

    def fake_connect(**params):
        captured_params.update(params)
        return FakeConnection()

    monkeypatch.setattr(snowflake.connector, "connect", fake_connect)
    patch_module._close_source_connection()

    patch()

    headers = _prepared_headers(network.SnowflakeAuth, "embucket-session-token")

    assert headers["Authorization"] == 'Snowflake Token="issued-spcs-token"'
    assert captured_params["account"] == "example-account"
    assert captured_params["validate_default_parameters"] is False
    assert captured_params["session_parameters"]["PYTHON_CONNECTOR_QUERY_RESULT_FORMAT"] == "json"
