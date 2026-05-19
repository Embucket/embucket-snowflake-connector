from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SPCS_AUTHORIZATION_ENV = "EMBUCKET_SPCS_AUTHORIZATION"
SPCS_TOKEN_ENV = "EMBUCKET_SPCS_TOKEN"
SPCS_TOKEN_FILE_ENV = "EMBUCKET_SPCS_TOKEN_FILE"
SPCS_TOKEN_COMMAND_ENV = "EMBUCKET_SPCS_TOKEN_COMMAND"
DEFAULT_TOKEN_FILE_NAME = "embucket_spcs_token"


class EmbucketSPCSConfigError(RuntimeError):
    pass


@dataclass
class _PatchState:
    authorization: str | None = None
    token: str | None = None
    token_file: str | None = None
    token_command: str | None = None
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


def _default_token_file_from_config() -> str | None:
    config_file = _arg_value("--config-file") or os.getenv("SNOW_CONFIG_FILE")
    if not config_file:
        return None

    candidate = Path(config_file).expanduser().resolve().with_name(DEFAULT_TOKEN_FILE_NAME)
    if candidate.is_file():
        return str(candidate)
    return None


def _resolve_spcs_authorization() -> str:
    authorization = _STATE.authorization or os.getenv(SPCS_AUTHORIZATION_ENV)
    if authorization:
        return authorization

    token = (
        _STATE.token
        or os.getenv(SPCS_TOKEN_ENV)
        or _read_token_command(_STATE.token_command or os.getenv(SPCS_TOKEN_COMMAND_ENV))
        or _read_token_file(
            _STATE.token_file
            or os.getenv(SPCS_TOKEN_FILE_ENV)
            or _default_token_file_from_config()
        )
    )
    if not token:
        raise EmbucketSPCSConfigError(
            f"Missing SPCS ingress token. Set {SPCS_TOKEN_ENV}, "
            f"{SPCS_TOKEN_FILE_ENV}, {SPCS_TOKEN_COMMAND_ENV}, or "
            f"{SPCS_AUTHORIZATION_ENV}."
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
    """

    spcs_token = kwargs.pop("spcs_token", None)
    spcs_authorization = kwargs.pop("spcs_authorization", None)
    spcs_token_file = kwargs.pop("spcs_token_file", None)
    spcs_token_command = kwargs.pop("spcs_token_command", None)
    patch(
        spcs_token=spcs_token,
        spcs_authorization=spcs_authorization,
        spcs_token_file=spcs_token_file,
        spcs_token_command=spcs_token_command,
    )

    import snowflake.connector

    return snowflake.connector.connect(*args, **kwargs)
