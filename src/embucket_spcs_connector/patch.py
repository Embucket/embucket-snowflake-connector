from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EMBUCKET_AUTHORIZATION_HEADER = "X-Embucket-Authorization"
SPCS_AUTHORIZATION_ENV = "EMBUCKET_SPCS_AUTHORIZATION"
SPCS_TOKEN_ENV = "EMBUCKET_SPCS_TOKEN"
SPCS_TOKEN_FILE_ENV = "EMBUCKET_SPCS_TOKEN_FILE"
DEFAULT_TOKEN_FILE_NAME = "embucket_spcs_token"


class EmbucketSPCSConfigError(RuntimeError):
    pass


@dataclass
class _PatchState:
    authorization: str | None = None
    token: str | None = None
    token_file: str | None = None
    embucket_header: str = EMBUCKET_AUTHORIZATION_HEADER
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
        or _read_token_file(
            _STATE.token_file
            or os.getenv(SPCS_TOKEN_FILE_ENV)
            or _default_token_file_from_config()
        )
    )
    if not token:
        raise EmbucketSPCSConfigError(
            f"Missing SPCS ingress token. Set {SPCS_TOKEN_ENV}, "
            f"{SPCS_TOKEN_FILE_ENV}, or {SPCS_AUTHORIZATION_ENV}."
        )
    return _snowflake_authorization(token)


def _make_auth_class(network_module: Any) -> type:
    no_token = network_module.NO_TOKEN
    authorization_header = network_module.HEADER_AUTHORIZATION_KEY
    embucket_header = _STATE.embucket_header

    class EmbucketSPCSSnowflakeAuth(network_module.AuthBase):
        """Requests auth adapter for Snowflake SPCS public ingress.

        SPCS consumes Authorization, so Embucket's own session token is sent in a
        separate header after login.
        """

        def __init__(self, token: str) -> None:
            self.token = token

        def __call__(self, request):
            request.headers.pop(authorization_header, None)
            request.headers[authorization_header] = _resolve_spcs_authorization()
            if self.token not in (no_token, None, "None"):
                request.headers[embucket_header] = _snowflake_authorization(self.token)
            return request

    return EmbucketSPCSSnowflakeAuth


def patch(
    *,
    spcs_token: str | None = None,
    spcs_authorization: str | None = None,
    spcs_token_file: str | None = None,
    embucket_header: str = EMBUCKET_AUTHORIZATION_HEADER,
) -> None:
    """Patch snowflake-connector-python for Embucket behind SPCS public ingress.

    The patch replaces the connector's SnowflakeAuth request auth adapter. The
    standard Authorization header is reserved for Snowflake SPCS ingress; the
    Embucket session token is forwarded in X-Embucket-Authorization.
    """

    import snowflake.connector.network as network

    _STATE.authorization = spcs_authorization
    _STATE.token = spcs_token
    _STATE.token_file = spcs_token_file
    _STATE.embucket_header = embucket_header

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
    - embucket_header
    """

    spcs_token = kwargs.pop("spcs_token", None)
    spcs_authorization = kwargs.pop("spcs_authorization", None)
    spcs_token_file = kwargs.pop("spcs_token_file", None)
    embucket_header = kwargs.pop("embucket_header", EMBUCKET_AUTHORIZATION_HEADER)
    patch(
        spcs_token=spcs_token,
        spcs_authorization=spcs_authorization,
        spcs_token_file=spcs_token_file,
        embucket_header=embucket_header,
    )

    import snowflake.connector

    return snowflake.connector.connect(*args, **kwargs)
