from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib

SPCS_AUTHORIZATION_ENV = "EMBUCKET_SPCS_AUTHORIZATION"
SPCS_TOKEN_ENV = "EMBUCKET_SPCS_TOKEN"
SPCS_TOKEN_FILE_ENV = "EMBUCKET_SPCS_TOKEN_FILE"
SPCS_TOKEN_COMMAND_ENV = "EMBUCKET_SPCS_TOKEN_COMMAND"
SPCS_TOKEN_CONNECTION_ENV = "EMBUCKET_SPCS_TOKEN_CONNECTION"
SPCS_TOKEN_CONFIG_FILE_ENV = "EMBUCKET_SPCS_TOKEN_CONFIG_FILE"
DEFAULT_TOKEN_FILE_NAME = "embucket_spcs_token"
DEFAULT_CONFIG_FILE = Path.home() / ".snowflake" / "config.toml"
SPCS_TOKEN_REFRESH_SKEW_SECONDS = 60


class EmbucketSPCSConfigError(RuntimeError):
    pass


@dataclass
class _TokenProvider:
    authorization: str | None = None
    token: str | None = None
    token_file: str | None = None
    token_command: str | None = None
    auto_token_source: tuple[str, str] | None = None
    source_connection: Any | None = None
    source_token: str | None = None
    source_token_expires_at: float = 0.0
    source_token_lock: Any = field(default_factory=threading.RLock)
    ingress_origin: tuple[str, str, int] | None = None

    def clone(self) -> _TokenProvider:
        return _TokenProvider(
            authorization=self.authorization,
            token=self.token,
            token_file=self.token_file,
            token_command=self.token_command,
            auto_token_source=self.auto_token_source,
        )


@dataclass
class _PatchState:
    default_provider: _TokenProvider | None = None
    original_auth_class: type | None = None
    original_connection_connect: Any | None = None
    original_connection_close: Any | None = None
    original_request_exec: Any | None = None
    original_session_manager_clone: Any | None = None
    install_lock: Any = field(default_factory=threading.RLock)
    patched: bool = False


_STATE = _PatchState()
_CURRENT_PROVIDER: ContextVar[_TokenProvider | None] = ContextVar(
    "embucket_spcs_current_provider", default=None
)
_BYPASS_SPCS_AUTH: ContextVar[bool] = ContextVar(
    "embucket_spcs_bypass_auth", default=False
)
_MANAGER_PROVIDER_ATTR = "_embucket_spcs_token_provider"


def _snowflake_authorization(token: str) -> str:
    return f'Snowflake Token="{token}"'


def _read_token_file(path: str | None) -> str | None:
    if not path:
        return None
    token = Path(path).expanduser().read_text(encoding="utf-8").strip()
    return token or None


def _read_token_command(command: str | None) -> str | None:
    if not command:
        return None
    token = subprocess.check_output(
        shlex.split(command),
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()
    return token or None


def _arg_value(name: str) -> str | None:
    for index, arg in enumerate(sys.argv):
        if arg == name and index + 1 < len(sys.argv):
            return sys.argv[index + 1]
        prefix = f"{name}="
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return None


def _snowflake_cli_config_file() -> str | None:
    config_file = _arg_value("--config-file") or os.getenv("SNOW_CONFIG_FILE")
    if not config_file:
        return str(DEFAULT_CONFIG_FILE) if DEFAULT_CONFIG_FILE.is_file() else None
    return config_file


def _snowflake_cli_connection_name(config: dict[str, Any]) -> str | None:
    return (
        _arg_value("-c")
        or _arg_value("--connection")
        or config.get("default_connection_name")
    )


def _read_toml_config(path: str) -> dict[str, Any]:
    try:
        return tomllib.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        raise EmbucketSPCSConfigError(f"Could not read Snowflake config {path}: {exc}") from exc


def _current_cli_connection_options() -> dict[str, Any]:
    config_file = _snowflake_cli_config_file()
    if not config_file:
        return {}

    config = _read_toml_config(config_file)
    connection_name = _snowflake_cli_connection_name(config)
    if not connection_name:
        return {}

    connections = config.get("connections") or {}
    options = connections.get(connection_name)
    return dict(options or {})


def _default_token_file_from_config() -> str | None:
    config_file = _snowflake_cli_config_file()
    if not config_file:
        return None

    candidate = Path(config_file).expanduser().resolve().with_name(DEFAULT_TOKEN_FILE_NAME)
    if candidate.is_file():
        return str(candidate)
    return None


def _auto_token_source(
    token_connection: str | None = None,
    token_config_file: str | None = None,
) -> tuple[str, str] | None:
    current_options = _current_cli_connection_options()
    connection_name = (
        token_connection
        or os.getenv(SPCS_TOKEN_CONNECTION_ENV)
        or current_options.get("spcs_token_connection")
    )
    if not connection_name:
        return None

    config_file = (
        token_config_file
        or os.getenv(SPCS_TOKEN_CONFIG_FILE_ENV)
        or current_options.get("spcs_token_config_file")
        or _snowflake_cli_config_file()
    )
    if not config_file:
        raise EmbucketSPCSConfigError(
            f"Missing Snowflake config for automatic SPCS token. Set "
            f"{SPCS_TOKEN_CONFIG_FILE_ENV} or spcs_token_config_file."
        )
    return str(config_file), str(connection_name)


def _connector_params_from_cli_config(config_file: str, connection_name: str) -> dict[str, Any]:
    config = _read_toml_config(config_file)
    options = (config.get("connections") or {}).get(connection_name)
    if not options:
        raise EmbucketSPCSConfigError(
            f"Missing Snowflake connection {connection_name!r} in {config_file}."
        )

    params = {
        key: value
        for key, value in dict(options).items()
        if not key.startswith("spcs_token_")
    }
    params.setdefault("client_session_keep_alive", True)
    params.setdefault("validate_default_parameters", False)

    session_parameters = dict(params.get("session_parameters") or {})
    session_parameters.setdefault("PYTHON_CONNECTOR_QUERY_RESULT_FORMAT", "json")
    params["session_parameters"] = session_parameters
    return params


def _close_source_connection(
    provider: _TokenProvider, *, clear_token: bool = True
) -> None:
    with provider.source_token_lock:
        connection = provider.source_connection
        provider.source_connection = None
        if clear_token:
            provider.source_token = None
            provider.source_token_expires_at = 0.0
    if connection is not None:
        _with_original_snowflake_auth(connection.close)


def _with_original_snowflake_auth(callback):
    bypass = _BYPASS_SPCS_AUTH.set(True)
    try:
        return callback()
    finally:
        _BYPASS_SPCS_AUTH.reset(bypass)


def _get_source_connection(
    provider: _TokenProvider, config_file: str, connection_name: str
):
    if provider.source_connection is not None:
        return provider.source_connection

    def connect():
        import snowflake.connector

        params = _connector_params_from_cli_config(config_file, connection_name)
        return snowflake.connector.connect(**params)

    provider.source_connection = _with_original_snowflake_auth(connect)
    return provider.source_connection


def _issue_source_token(
    provider: _TokenProvider,
    config_file: str,
    connection_name: str,
    *,
    force_refresh: bool = False,
) -> str:
    with provider.source_token_lock:
        if (
            not force_refresh
            and provider.source_token
            and time.monotonic() < provider.source_token_expires_at
        ):
            return provider.source_token

        connection = _get_source_connection(provider, config_file, connection_name)
        try:
            token_data = _with_original_snowflake_auth(
                lambda: connection._rest._token_request("ISSUE")
            )
        except Exception:
            _close_source_connection(provider)
            connection = _get_source_connection(provider, config_file, connection_name)
            token_data = _with_original_snowflake_auth(
                lambda: connection._rest._token_request("ISSUE")
            )

        data = token_data.get("data") or {}
        token = data.get("sessionToken")
        if not token:
            raise EmbucketSPCSConfigError(
                f"Could not issue SPCS ingress token from Snowflake connection "
                f"{connection_name!r}."
            )

        validity = int(data.get("validityInSecondsST") or 3600)
        provider.source_token = token
        provider.source_token_expires_at = time.monotonic() + max(
            0, validity - SPCS_TOKEN_REFRESH_SKEW_SECONDS
        )
        return token


def _read_auto_token(
    provider: _TokenProvider, *, force_refresh: bool = False
) -> str | None:
    source = provider.auto_token_source
    if not source:
        return None
    return _issue_source_token(provider, *source, force_refresh=force_refresh)


def _resolve_spcs_authorization(
    provider: _TokenProvider, *, force_refresh: bool = False
) -> str:
    authorization = provider.authorization
    if authorization:
        return authorization

    token = (
        provider.token
        or _read_token_command(provider.token_command)
        or _read_auto_token(provider, force_refresh=force_refresh)
        or _read_token_file(provider.token_file)
    )
    if not token:
        raise EmbucketSPCSConfigError(
            f"Missing SPCS ingress token. Set {SPCS_TOKEN_ENV}, "
            f"{SPCS_TOKEN_FILE_ENV}, {SPCS_TOKEN_COMMAND_ENV}, "
            f"{SPCS_TOKEN_CONNECTION_ENV}, or {SPCS_AUTHORIZATION_ENV}."
        )
    if token.startswith("Snowflake Token="):
        return token
    return _snowflake_authorization(token)


def _spcs_ingress_origin(url: str) -> tuple[str, str, int] | None:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname.endswith(".snowflakecomputing.app"):
        return None
    try:
        port = parsed.port or 443
    except ValueError:
        return None
    return parsed.scheme, hostname, port


def _bind_spcs_ingress(provider: _TokenProvider, url: str) -> bool:
    origin = _spcs_ingress_origin(url)
    if origin is None:
        return False
    with provider.source_token_lock:
        if provider.ingress_origin is None:
            provider.ingress_origin = origin
        return provider.ingress_origin == origin


def _is_bound_spcs_ingress(provider: _TokenProvider, url: str) -> bool:
    origin = _spcs_ingress_origin(url)
    with provider.source_token_lock:
        return provider.ingress_origin is not None and provider.ingress_origin == origin


def _make_auth_class(network_module: Any) -> type:
    authorization_header = network_module.HEADER_AUTHORIZATION_KEY
    original_auth_class = _STATE.original_auth_class

    class EmbucketSPCSSnowflakeAuth(network_module.AuthBase):
        """Requests auth adapter for Snowflake SPCS public ingress.

        SPCS consumes Authorization, so keep the Snowflake ingress token there
        on every request and let Rustice derive its session from SPCS caller
        context headers.
        """

        def __init__(self, token: str) -> None:
            self.token = token
            self._provider = _CURRENT_PROVIDER.get()
            self._delegate = (
                original_auth_class(token)
                if (_BYPASS_SPCS_AUTH.get() or self._provider is None)
                and original_auth_class is not None
                else None
            )

        def __call__(self, request):
            if self._delegate is not None:
                return self._delegate(request)
            provider = self._provider
            if provider is None or not _bind_spcs_ingress(provider, request.url):
                raise EmbucketSPCSConfigError(
                    f"Refusing to send SPCS authorization to unbound URL {request.url!r}."
                )
            request.headers.pop(authorization_header, None)
            force_refresh = "/session/v1/login-request" in request.url
            request.headers[authorization_header] = _resolve_spcs_authorization(
                provider, force_refresh=force_refresh
            )
            return request

    return EmbucketSPCSSnowflakeAuth


def _make_chunk_auth(provider: _TokenProvider, fallback_auth: Any = None):
    """Inject current SPCS auth only into same-ingress result chunk GETs."""

    def authenticate(request):
        if callable(fallback_auth):
            request = fallback_auth(request)
        elif isinstance(fallback_auth, tuple) and len(fallback_auth) == 2:
            from snowflake.connector.vendored.requests.auth import HTTPBasicAuth

            request = HTTPBasicAuth(*fallback_auth)(request)
        if _is_bound_spcs_ingress(provider, request.url):
            request.headers["Authorization"] = _resolve_spcs_authorization(provider)
        return request

    return authenticate


def _invalidate_rejected_spcs_token(provider: _TokenProvider):
    def invalidate(response, *args, **kwargs):
        del args, kwargs
        request = getattr(response, "request", None)
        request_url = getattr(request, "url", None) or response.url
        rejected_authorization = (
            request.headers.get("Authorization") if request is not None else None
        )
        if (
            response.status_code == 401
            and _is_bound_spcs_ingress(provider, request_url)
            and rejected_authorization is not None
        ):
            with provider.source_token_lock:
                current = provider.source_token
                if (
                    current is not None
                    and rejected_authorization == _snowflake_authorization(current)
                ):
                    provider.source_token = None
                    provider.source_token_expires_at = 0.0
        return response

    return invalidate


def _patch_session_manager() -> None:
    from snowflake.connector.session_manager import SessionManager

    original_make_session = SessionManager.make_session
    original_clone = SessionManager.clone
    _STATE.original_session_manager_clone = original_clone

    def make_session(manager):
        session = original_make_session(manager)
        if _BYPASS_SPCS_AUTH.get():
            return session
        provider = getattr(manager, _MANAGER_PROVIDER_ATTR, None) or _CURRENT_PROVIDER.get()
        if provider is None:
            return session
        setattr(manager, _MANAGER_PROVIDER_ATTR, provider)
        session.auth = _make_chunk_auth(provider, session.auth)
        session.hooks["response"].append(_invalidate_rejected_spcs_token(provider))
        return session

    def clone(manager, **http_config_overrides):
        cloned = original_clone(manager, **http_config_overrides)
        provider = getattr(manager, _MANAGER_PROVIDER_ATTR, None)
        if provider is not None:
            setattr(cloned, _MANAGER_PROVIDER_ATTR, provider)
        return cloned

    SessionManager.make_session = make_session
    SessionManager.clone = clone


def _request_exec_with_provider(rest, callback, *args, **kwargs):
    if _BYPASS_SPCS_AUTH.get():
        return callback(rest, *args, **kwargs)
    manager = getattr(rest, "session_manager", None)
    provider = getattr(manager, _MANAGER_PROVIDER_ATTR, None) or _CURRENT_PROVIDER.get()
    if provider is not None and manager is not None:
        setattr(manager, _MANAGER_PROVIDER_ATTR, provider)
    current = _CURRENT_PROVIDER.set(provider)
    try:
        return callback(rest, *args, **kwargs)
    finally:
        _CURRENT_PROVIDER.reset(current)


def _connection_close_with_provider(connection, callback, *args, **kwargs):
    manager = getattr(connection, "_session_manager", None)
    provider = getattr(connection, _MANAGER_PROVIDER_ATTR, None) or getattr(
        manager, _MANAGER_PROVIDER_ATTR, None
    )
    try:
        return callback(connection, *args, **kwargs)
    finally:
        if provider is not None:
            _close_source_connection(provider, clear_token=False)


def _patch_connection_provider_context() -> None:
    from snowflake.connector.connection import SnowflakeConnection
    from snowflake.connector.network import SnowflakeRestful

    original_connect = SnowflakeConnection.connect
    original_close = SnowflakeConnection.close
    original_request_exec = SnowflakeRestful._request_exec
    _STATE.original_connection_connect = original_connect
    _STATE.original_connection_close = original_close
    _STATE.original_request_exec = original_request_exec

    def connection_connect(connection, **kwargs):
        if _BYPASS_SPCS_AUTH.get():
            return original_connect(connection, **kwargs)
        inherited = _CURRENT_PROVIDER.get()
        provider = inherited or (
            _STATE.default_provider.clone() if _STATE.default_provider is not None else None
        )
        current = _CURRENT_PROVIDER.set(provider)
        try:
            result = original_connect(connection, **kwargs)
            manager = getattr(connection, "_session_manager", None)
            if provider is not None and manager is not None:
                setattr(manager, _MANAGER_PROVIDER_ATTR, provider)
                setattr(connection, _MANAGER_PROVIDER_ATTR, provider)
            return result
        finally:
            _CURRENT_PROVIDER.reset(current)

    def connection_close(connection, *args, **kwargs):
        return _connection_close_with_provider(connection, original_close, *args, **kwargs)

    def request_exec(rest, *args, **kwargs):
        return _request_exec_with_provider(rest, original_request_exec, *args, **kwargs)

    SnowflakeConnection.connect = connection_connect
    SnowflakeConnection.close = connection_close
    SnowflakeRestful._request_exec = request_exec


def _build_provider(
    *,
    spcs_token: str | None = None,
    spcs_authorization: str | None = None,
    spcs_token_file: str | None = None,
    spcs_token_command: str | None = None,
    spcs_token_connection: str | None = None,
    spcs_token_config_file: str | None = None,
) -> _TokenProvider:
    return _TokenProvider(
        authorization=spcs_authorization or os.getenv(SPCS_AUTHORIZATION_ENV),
        token=spcs_token or os.getenv(SPCS_TOKEN_ENV),
        token_file=(
            spcs_token_file
            or os.getenv(SPCS_TOKEN_FILE_ENV)
            or _default_token_file_from_config()
        ),
        token_command=spcs_token_command or os.getenv(SPCS_TOKEN_COMMAND_ENV),
        auto_token_source=_auto_token_source(
            spcs_token_connection, spcs_token_config_file
        ),
    )


def patch(
    *,
    spcs_token: str | None = None,
    spcs_authorization: str | None = None,
    spcs_token_file: str | None = None,
    spcs_token_command: str | None = None,
    spcs_token_connection: str | None = None,
    spcs_token_config_file: str | None = None,
) -> None:
    """Patch snowflake-connector-python for Embucket behind SPCS public ingress.

    The patch replaces the connector's SnowflakeAuth request auth adapter. The
    standard Authorization header is reserved for Snowflake SPCS ingress.
    Rustice derives its own session from Snowflake's SPCS caller context headers.
    """

    import snowflake.connector.network as network

    provider = _build_provider(
        spcs_token=spcs_token,
        spcs_authorization=spcs_authorization,
        spcs_token_file=spcs_token_file,
        spcs_token_command=spcs_token_command,
        spcs_token_connection=spcs_token_connection,
        spcs_token_config_file=spcs_token_config_file,
    )

    with _STATE.install_lock:
        _STATE.default_provider = provider
        if _STATE.patched:
            return

        _STATE.original_auth_class = network.SnowflakeAuth
        network.SnowflakeAuth = _make_auth_class(network)
        _patch_session_manager()
        _patch_connection_provider_context()
        _STATE.patched = True


def is_patched() -> bool:
    return _STATE.patched


def connect(*args: Any, **kwargs: Any):
    """Connect using snowflake-connector-python with the SPCS patch installed.

    Extra keyword arguments consumed by this wrapper:
    - spcs_token
    - spcs_authorization
    - spcs_token_file
    - spcs_token_command
    - spcs_token_connection
    - spcs_token_config_file
    """

    spcs_token = kwargs.pop("spcs_token", None)
    spcs_authorization = kwargs.pop("spcs_authorization", None)
    spcs_token_file = kwargs.pop("spcs_token_file", None)
    spcs_token_command = kwargs.pop("spcs_token_command", None)
    spcs_token_connection = kwargs.pop("spcs_token_connection", None)
    spcs_token_config_file = kwargs.pop("spcs_token_config_file", None)
    provider = _build_provider(
        spcs_token=spcs_token,
        spcs_authorization=spcs_authorization,
        spcs_token_file=spcs_token_file,
        spcs_token_command=spcs_token_command,
        spcs_token_connection=spcs_token_connection,
        spcs_token_config_file=spcs_token_config_file,
    )
    patch()

    import snowflake.connector

    current = _CURRENT_PROVIDER.set(provider)
    try:
        return snowflake.connector.connect(*args, **kwargs)
    finally:
        _CURRENT_PROVIDER.reset(current)
