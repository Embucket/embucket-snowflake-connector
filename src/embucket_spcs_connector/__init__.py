from __future__ import annotations

from .patch import (
    EMBUCKET_AUTHORIZATION_HEADER,
    SPCS_AUTHORIZATION_ENV,
    SPCS_TOKEN_ENV,
    SPCS_TOKEN_FILE_ENV,
    connect,
    is_patched,
    patch,
)

__all__ = [
    "EMBUCKET_AUTHORIZATION_HEADER",
    "SPCS_AUTHORIZATION_ENV",
    "SPCS_TOKEN_ENV",
    "SPCS_TOKEN_FILE_ENV",
    "connect",
    "is_patched",
    "patch",
]
