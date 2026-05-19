from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
import atexit
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
class _PatchState:
    authorization: str | None = None
    token: str | None = None
    token_file: str | None = None
    token_command: str | None = None
    token_connection: str | None = None
    token_config_file: str | None = None
    source_connection: Any | None = None
    source_token: str | None = None
    source_token_expires_at: float = 0.0
    original_auth_class: type | None = None
    patched: bool = False


_STATE = _PatchState()


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


def _auto_token_source() -> tuple[str, str] | None:
    current_options = _current_cli_connection_options()
    connection_name = (
        _STATE.token_connection
        or os.getenv(SPCS_TOKEN_CONNECTION_ENV)
        or current_options.get("spcs_token_connection")
    )
    if not connection_name:
        return None

    config_file = (
        _STATE.token_config_file
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


def _close_source_connection() -> None:
    connection = _STATE.source_connection
    _STATE.source_connection = None
    _STATE.source_token = None
    _STATE.source_token_expires_at = 0.0
    if connection is not None:
        connection.close()


def _with_original_snowflake_auth(callback):
    import snowflake.connector.network as network

    patched_auth_class = network.SnowflakeAuth
    if _STATE.original_auth_class is not None:
        network.SnowflakeAuth = _STATE.original_auth_class
    try:
        return callback()
    finally:
        network.SnowflakeAuth = patched_auth_class


def _get_source_connection(config_file: str, connection_name: str):
    if _STATE.source_connection is not None:
        return _STATE.source_connection

    def connect():
        import snowflake.connector

        params = _connector_params_from_cli_config(config_file, connection_name)
        return snowflake.connector.connect(**params)

    _STATE.source_connection = _with_original_snowflake_auth(connect)
    atexit.register(_close_source_connection)
    return _STATE.source_connection


def _issue_source_token(config_file: str, connection_name: str) -> str:
    if _STATE.source_token and time.monotonic() < _STATE.source_token_expires_at:
        return _STATE.source_token

    connection = _get_source_connection(config_file, connection_name)
    try:
        token_data = _with_original_snowflake_auth(
            lambda: connection._rest._token_request("ISSUE")
        )
    except Exception:
        _close_source_connection()
        connection = _get_source_connection(config_file, connection_name)
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
    _STATE.source_token = token
    _STATE.source_token_expires_at = time.monotonic() + max(
        0, validity - SPCS_TOKEN_REFRESH_SKEW_SECONDS
    )
    return token


def _read_auto_token() -> str | None:
    source = _auto_token_source()
    if not source:
        return None
    return _issue_source_token(*source)


def _resolve_spcs_authorization() -> str:
    authorization = _STATE.authorization or os.getenv(SPCS_AUTHORIZATION_ENV)
    if authorization:
        return authorization

    token = (
        _STATE.token
        or os.getenv(SPCS_TOKEN_ENV)
        or _read_token_command(_STATE.token_command or os.getenv(SPCS_TOKEN_COMMAND_ENV))
        or _read_auto_token()
        or _read_token_file(
            _STATE.token_file
            or os.getenv(SPCS_TOKEN_FILE_ENV)
            or _default_token_file_from_config()
        )
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


def _make_auth_class(network_module: Any) -> type:
    authorization_header = network_module.HEADER_AUTHORIZATION_KEY

    class EmbucketSPCSSnowflakeAuth(network_module.AuthBase):
        """Requests auth adapter for Snowflake SPCS public ingress.

        SPCS consumes Authorization, so keep the Snowflake ingress token there
        on every request and let Rustice derive its session from SPCS caller
        context headers.
        """

        def __init__(self, token: str) -> None:
            self.token = token

        def __call__(self, request):
            request.headers.pop(authorization_header, None)
            request.headers[authorization_header] = _resolve_spcs_authorization()
            return request

    return EmbucketSPCSSnowflakeAuth


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

    _STATE.authorization = spcs_authorization
    _STATE.token = spcs_token
    _STATE.token_file = spcs_token_file
    _STATE.token_command = spcs_token_command
    _STATE.token_connection = spcs_token_connection
    _STATE.token_config_file = spcs_token_config_file

    if _STATE.patched:
        return

    _STATE.original_auth_class = network.SnowflakeAuth
    network.SnowflakeAuth = _make_auth_class(network)
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
    patch(
        spcs_token=spcs_token,
        spcs_authorization=spcs_authorization,
        spcs_token_file=spcs_token_file,
        spcs_token_command=spcs_token_command,
        spcs_token_connection=spcs_token_connection,
        spcs_token_config_file=spcs_token_config_file,
    )

    import snowflake.connector

    return snowflake.connector.connect(*args, **kwargs)
