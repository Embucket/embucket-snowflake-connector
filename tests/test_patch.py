from __future__ import annotations

import sys
import importlib
from concurrent.futures import ThreadPoolExecutor

from requests import Request

from embucket_spcs_connector import patch


def _prepared_headers(
    auth,
    token="embucket-session-token",
    url="https://example.snowflakecomputing.app",
):
    request = Request("POST", url)
    prepared = request.prepare()
    patch_module = importlib.import_module("embucket_spcs_connector.patch")
    current = patch_module._CURRENT_PROVIDER.set(patch_module._STATE.default_provider)
    try:
        auth_instance = auth(token)
        return auth_instance(prepared).headers
    finally:
        patch_module._CURRENT_PROVIDER.reset(current)


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


def test_auth_without_connection_provider_delegates_to_original_snowflake_auth():
    import snowflake.connector.network as network

    patch(spcs_token="spcs-token")
    request = Request(
        "POST", "https://account.snowflakecomputing.com/session/heartbeat"
    ).prepare()

    prepared = network.SnowflakeAuth("backend-session-token")(request)

    assert prepared.headers["Authorization"] == (
        'Snowflake Token="backend-session-token"'
    )


def test_auto_token_source_manager_does_not_capture_target_provider():
    import snowflake.connector.network as network
    from snowflake.connector.session_manager import SessionManager

    patch_module = importlib.import_module("embucket_spcs_connector.patch")
    patch(spcs_token="default-spcs-token")
    target_provider = patch_module._TokenProvider(token="target-spcs-token")
    current = patch_module._CURRENT_PROVIDER.set(target_provider)
    bypass = patch_module._BYPASS_SPCS_AUTH.set(True)
    try:
        source_manager = SessionManager(use_pooling=False)
        with source_manager.use_requests_session() as session:
            assert session.auth is None
    finally:
        patch_module._BYPASS_SPCS_AUTH.reset(bypass)
        patch_module._CURRENT_PROVIDER.reset(current)

    assert not hasattr(source_manager, patch_module._MANAGER_PROVIDER_ATTR)

    class FakeSourceRest:
        session_manager = source_manager

    def heartbeat(rest):
        del rest
        request = Request(
            "POST", "https://account.snowflakecomputing.com/session/heartbeat"
        ).prepare()
        return network.SnowflakeAuth("source-session-token")(request)

    request = patch_module._request_exec_with_provider(FakeSourceRest(), heartbeat)
    assert request.headers["Authorization"] == 'Snowflake Token="source-session-token"'


def test_login_request_forces_auto_token_refresh(monkeypatch):
    import snowflake.connector.network as network
    patch_module = importlib.import_module("embucket_spcs_connector.patch")

    calls = []

    def fake_resolve_spcs_authorization(provider, *, force_refresh=False):
        del provider
        calls.append(force_refresh)
        return 'Snowflake Token="issued-spcs-token"'

    monkeypatch.setattr(
        patch_module,
        "_resolve_spcs_authorization",
        fake_resolve_spcs_authorization,
    )
    patch(spcs_token_connection="snowflake")

    login_headers = _prepared_headers(
        network.SnowflakeAuth,
        network.NO_TOKEN,
        "https://example.snowflakecomputing.app/session/v1/login-request",
    )
    query_headers = _prepared_headers(
        network.SnowflakeAuth,
        "embucket-session-token",
        "https://example.snowflakecomputing.app/queries/v1/query-request",
    )

    assert login_headers["Authorization"] == 'Snowflake Token="issued-spcs-token"'
    assert query_headers["Authorization"] == 'Snowflake Token="issued-spcs-token"'
    assert calls == [True, False]


def test_initial_login_request_keeps_explicit_connection_provider():
    import snowflake.connector.network as network
    from snowflake.connector.session_manager import SessionManager

    patch_module = importlib.import_module("embucket_spcs_connector.patch")
    patch(spcs_token="default-spcs-token")
    explicit_provider = patch_module._TokenProvider(token="explicit-token")
    manager = SessionManager(use_pooling=False)

    class FakeRest:
        session_manager = manager

    def execute(rest):
        del rest
        request = Request(
            "POST",
            "https://explicit.snowflakecomputing.app/session/v1/login-request",
        ).prepare()
        return network.SnowflakeAuth(network.NO_TOKEN)(request)

    current = patch_module._CURRENT_PROVIDER.set(explicit_provider)
    try:
        request = patch_module._request_exec_with_provider(FakeRest(), execute)
    finally:
        patch_module._CURRENT_PROVIDER.reset(current)

    assert request.headers["Authorization"] == 'Snowflake Token="explicit-token"'
    assert (
        getattr(manager, patch_module._MANAGER_PROVIDER_ATTR, None)
        is explicit_provider
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


def test_result_chunk_download_uses_rotated_token_only_for_exact_ingress(tmp_path):
    import snowflake.connector.network as network
    from snowflake.connector.session_manager import SessionManager

    token_file = tmp_path / "embucket_spcs_token"
    token_file.write_text("spcs-token-1", encoding="utf-8")
    patch(spcs_token_file=str(token_file))

    ingress = "https://example.snowflakecomputing.app"
    _prepared_headers(
        network.SnowflakeAuth,
        "embucket-session-token",
        f"{ingress}/queries/v1/query-request",
    )
    patch_module = importlib.import_module("embucket_spcs_connector.patch")
    manager = SessionManager(use_pooling=False)
    setattr(
        manager,
        patch_module._MANAGER_PROVIDER_ATTR,
        patch_module._STATE.default_provider,
    )
    session = manager.make_session()
    try:
        first = session.prepare_request(Request("GET", f"{ingress}/queries/q/chunks/c1"))
        assert first.headers["Authorization"] == 'Snowflake Token="spcs-token-1"'

        token_file.write_text("spcs-token-2", encoding="utf-8")
        second = session.prepare_request(Request("GET", f"{ingress}/queries/q/chunks/c2"))
        assert second.headers["Authorization"] == 'Snowflake Token="spcs-token-2"'

        for external_url in (
            "https://example.snowflakecomputing.app.evil.test/chunk",
            "https://bucket.s3.us-east-2.amazonaws.com/chunk",
            "https://other.snowflakecomputing.app/chunk",
        ):
            external = session.prepare_request(Request("GET", external_url))
            assert "Authorization" not in external.headers
    finally:
        session.close()


def test_result_chunk_unauthorized_only_invalidates_token_used_by_request():
    from snowflake.connector.vendored.requests import Response

    patch_module = importlib.import_module("embucket_spcs_connector.patch")
    ingress = "https://example.snowflakecomputing.app"
    provider = patch_module._TokenProvider(
        auto_token_source=("config.toml", "snowflake"),
        source_token="new-token",
        source_token_expires_at=float("inf"),
    )
    assert patch_module._bind_spcs_ingress(provider, ingress)
    hook = patch_module._invalidate_rejected_spcs_token(provider)

    delayed = Response()
    delayed.status_code = 401
    delayed.url = f"{ingress}/queries/q/chunks/old"
    delayed.request = Request(
        "GET",
        delayed.url,
        headers={"Authorization": 'Snowflake Token="old-token"'},
    ).prepare()
    hook(delayed)
    assert provider.source_token == "new-token"

    current = Response()
    current.status_code = 401
    current.url = f"{ingress}/queries/q/chunks/current"
    current.request = Request(
        "GET",
        current.url,
        headers={"Authorization": 'Snowflake Token="new-token"'},
    ).prepare()
    hook(current)
    assert provider.source_token is None
    assert provider.source_token_expires_at == 0.0


def test_result_chunk_sessions_keep_connection_bound_tokens():
    from snowflake.connector.session_manager import SessionManager

    patch_module = importlib.import_module("embucket_spcs_connector.patch")
    patch(spcs_token="default-spcs-token")
    ingress = "https://shared.snowflakecomputing.app"
    other_ingress = "https://other.snowflakecomputing.app"
    provider_a = patch_module._TokenProvider(token="token-a")
    provider_b = patch_module._TokenProvider(token="token-b")
    assert patch_module._bind_spcs_ingress(provider_a, ingress)
    assert patch_module._bind_spcs_ingress(provider_b, ingress)

    def manager_for(provider):
        manager = SessionManager(use_pooling=False)
        setattr(manager, patch_module._MANAGER_PROVIDER_ATTR, provider)
        return manager

    manager_a = manager_for(provider_a)
    manager_b = manager_for(provider_b)
    clone_a = manager_a.clone(use_pooling=False)

    def authorization(manager, url):
        with manager.use_requests_session(url) as session:
            return session.prepare_request(Request("GET", url)).headers.get("Authorization")

    with ThreadPoolExecutor(max_workers=3) as pool:
        headers = list(
            pool.map(
                lambda item: authorization(*item),
                [
                    (manager_a, f"{ingress}/queries/q/chunks/1"),
                    (clone_a, f"{ingress}/queries/q/chunks/2"),
                    (manager_b, f"{ingress}/queries/q/chunks/3"),
                ],
            )
        )

    assert headers == [
        'Snowflake Token="token-a"',
        'Snowflake Token="token-a"',
        'Snowflake Token="token-b"',
    ]
    assert authorization(manager_a, f"{other_ingress}/queries/q/chunks/4") is None
    assert authorization(manager_b, f"{other_ingress}/queries/q/chunks/5") is None


def test_connector_result_batch_download_uses_cloned_connection_provider():
    from snowflake.connector.result_batch import JSONResultBatch, RemoteChunkInfo
    from snowflake.connector.session_manager import HttpConfig, SessionManager
    from snowflake.connector.vendored.requests import Response
    from snowflake.connector.vendored.requests.adapters import BaseAdapter

    patch_module = importlib.import_module("embucket_spcs_connector.patch")
    patch(spcs_token="default-spcs-token")
    ingress = "https://example.snowflakecomputing.app"
    provider = patch_module._TokenProvider(token="connection-token")
    assert patch_module._bind_spcs_ingress(provider, ingress)
    requests = []

    class RecordingAdapter(BaseAdapter):
        def send(self, request, **kwargs):
            del kwargs
            requests.append(request)
            response = Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response._content = b"chunk"
            return response

        def close(self):
            pass

    manager = SessionManager(
        HttpConfig(
            adapter_factory=lambda **kwargs: RecordingAdapter(),
            use_pooling=False,
        )
    )
    setattr(manager, patch_module._MANAGER_PROVIDER_ATTR, provider)
    cloned = manager.clone(use_pooling=False)
    batch = JSONResultBatch(
        rowcount=1,
        chunk_headers=None,
        remote_chunk_info=RemoteChunkInfo(
            url=f"{ingress}/queries/q/chunks/c1",
            uncompressedSize=5,
            compressedSize=5,
        ),
        schema=[],
        column_converters=[],
        use_dict_result=False,
        session_manager=cloned,
    )

    response = batch._download()

    assert response.status_code == 200
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == 'Snowflake Token="connection-token"'


def test_connector_result_batch_retries_401_with_refreshed_auto_token(monkeypatch):
    import snowflake.connector.result_batch as result_batch_module
    from snowflake.connector.result_batch import JSONResultBatch, RemoteChunkInfo
    from snowflake.connector.session_manager import HttpConfig, SessionManager
    from snowflake.connector.vendored.requests import Response
    from snowflake.connector.vendored.requests.adapters import BaseAdapter

    patch_module = importlib.import_module("embucket_spcs_connector.patch")
    patch(spcs_token="default-spcs-token")
    ingress = "https://example.snowflakecomputing.app"
    provider = patch_module._TokenProvider(
        auto_token_source=("config.toml", "snowflake"),
        source_token="stale-token",
        source_token_expires_at=float("inf"),
    )
    assert patch_module._bind_spcs_ingress(provider, ingress)
    requests = []

    def issue_token(token_provider, *args, force_refresh=False):
        del args, force_refresh
        if token_provider.source_token is not None:
            return token_provider.source_token
        token_provider.source_token = "fresh-token"
        token_provider.source_token_expires_at = float("inf")
        return token_provider.source_token

    monkeypatch.setattr(patch_module, "_issue_source_token", issue_token)
    monkeypatch.setattr(result_batch_module.time, "sleep", lambda _: None)

    class RefreshingAdapter(BaseAdapter):
        def send(self, request, **kwargs):
            del kwargs
            requests.append(request.headers.get("Authorization"))
            response = Response()
            response.status_code = 401 if len(requests) == 1 else 200
            response.url = request.url
            response.request = request
            response._content = b"chunk"
            return response

        def close(self):
            pass

    manager = SessionManager(
        HttpConfig(
            adapter_factory=lambda **kwargs: RefreshingAdapter(),
            use_pooling=False,
        )
    )
    setattr(manager, patch_module._MANAGER_PROVIDER_ATTR, provider)
    batch = JSONResultBatch(
        rowcount=1,
        chunk_headers=None,
        remote_chunk_info=RemoteChunkInfo(
            url=f"{ingress}/queries/q/chunks/c1",
            uncompressedSize=5,
            compressedSize=5,
        ),
        schema=[],
        column_converters=[],
        use_dict_result=False,
        session_manager=manager.clone(use_pooling=False),
    )

    response = batch._download()

    assert response.status_code == 200
    assert requests == [
        'Snowflake Token="stale-token"',
        'Snowflake Token="fresh-token"',
    ]


def test_target_connection_close_releases_auto_token_source_connection():
    patch_module = importlib.import_module("embucket_spcs_connector.patch")
    closed = []

    class SourceConnection:
        def close(self):
            assert patch_module._BYPASS_SPCS_AUTH.get()
            closed.append(True)

    provider = patch_module._TokenProvider(
        auto_token_source=("config.toml", "snowflake"),
        source_connection=SourceConnection(),
        source_token="issued-token",
        source_token_expires_at=float("inf"),
    )

    class TargetConnection:
        pass

    target = TargetConnection()
    setattr(target, patch_module._MANAGER_PROVIDER_ATTR, provider)
    patch_module._connection_close_with_provider(target, lambda connection: None)

    assert closed == [True]
    assert provider.source_connection is None
    assert provider.source_token == "issued-token"


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
    patch()
    provider = patch_module._STATE.default_provider
    patch_module._close_source_connection(provider)

    headers = _prepared_headers(network.SnowflakeAuth, "embucket-session-token")

    assert headers["Authorization"] == 'Snowflake Token="issued-spcs-token"'
    assert captured_params["account"] == "example-account"
    assert captured_params["validate_default_parameters"] is False
    assert captured_params["session_parameters"]["PYTHON_CONNECTOR_QUERY_RESULT_FORMAT"] == "json"
