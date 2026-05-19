from __future__ import annotations

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


def test_query_request_uses_spcs_and_embucket_authorization():
    import snowflake.connector.network as network

    patch(spcs_token="spcs-token")

    headers = _prepared_headers(network.SnowflakeAuth, "embucket-session-token")

    assert headers["Authorization"] == 'Snowflake Token="spcs-token"'
    assert (
        headers["X-Embucket-Authorization"]
        == 'Snowflake Token="embucket-session-token"'
    )


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
    assert (
        headers["X-Embucket-Authorization"]
        == 'Snowflake Token="embucket-session-token"'
    )
