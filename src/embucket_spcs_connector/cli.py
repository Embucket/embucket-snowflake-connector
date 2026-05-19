from __future__ import annotations

import sys

from .patch import EmbucketSPCSConfigError, patch


def main() -> None:
    try:
        patch()
    except EmbucketSPCSConfigError as exc:
        print(f"embucket-snow: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    from snowflake.cli._app.__main__ import main as snow_main

    snow_main()
