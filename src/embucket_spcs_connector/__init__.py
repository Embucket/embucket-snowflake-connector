from __future__ import annotations

from .patch import (
    SPCS_AUTHORIZATION_ENV,
    SPCS_TOKEN_COMMAND_ENV,
    SPCS_TOKEN_CONFIG_FILE_ENV,
    SPCS_TOKEN_CONNECTION_ENV,
    SPCS_TOKEN_ENV,
    SPCS_TOKEN_FILE_ENV,
    connect,
    is_patched,
    patch,
)

__all__ = [
    "SPCS_AUTHORIZATION_ENV",
    "SPCS_TOKEN_COMMAND_ENV",
    "SPCS_TOKEN_CONFIG_FILE_ENV",
    "SPCS_TOKEN_CONNECTION_ENV",
    "SPCS_TOKEN_ENV",
    "SPCS_TOKEN_FILE_ENV",
    "connect",
    "is_patched",
    "patch",
]
