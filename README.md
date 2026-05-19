# Embucket Snowflake Connector Patch

This package adapts `snowflake-connector-python` and Snowflake CLI for Embucket/Rustice running behind Snowpark Container Services (SPCS) public ingress.

SPCS public ingress consumes the standard Snowflake `Authorization` header. Embucket still needs its own session token after `/session/v1/login-request`, so this package sends:

```http
Authorization: Snowflake Token="<spcs-token>"
X-Embucket-Authorization: Snowflake Token="<embucket-session-token>"
```

## Install for Development

```bash
cd /home/artem/work/reps/github.com/embucket/embucket-snowflake-connector
python -m pip install -e ".[cli]"
```

## Snowflake CLI Wrapper

Create a normal Snowflake CLI connection that points to the SPCS public ingress host:

```toml
[connections.embucket_spcs]
host = "<ingress-host-without-https>"
protocol = "https"
port = 443
account = "embucket"
user = "embucket"
password = "embucket"
database = "embucket"
schema = "public"
warehouse = "embucket"
```

Get the ingress host with:

```bash
snow --config-file /path/to/config.toml sql -c snowflake \
  -q "SHOW ENDPOINTS IN SERVICE RUSTICE_APP.PUBLIC.RUSTICE_SERVICE"
```

Run Snowflake CLI through the wrapper:

```bash
export EMBUCKET_SPCS_TOKEN="<pat-or-oauth-token-for-spcs-ingress>"

embucket-snow --config-file /path/to/config.toml sql -c embucket_spcs \
  -q "SELECT * FROM embucket.public.smoke"
```

You can also provide the full ingress authorization header:

```bash
export EMBUCKET_SPCS_AUTHORIZATION='Snowflake Token="<pat-or-oauth-token-for-spcs-ingress>"'
```

or read the token from a file:

```bash
export EMBUCKET_SPCS_TOKEN_FILE=/path/to/spcs-token.txt
```

## Python Connector

```python
from embucket_spcs_connector import connect

conn = connect(
    host="<ingress-host-without-https>",
    protocol="https",
    port=443,
    account="embucket",
    user="embucket",
    password="embucket",
    database="embucket",
    schema="public",
    warehouse="embucket",
    spcs_token="<pat-or-oauth-token-for-spcs-ingress>",
)

cur = conn.cursor()
cur.execute("SELECT * FROM embucket.public.smoke")
print(cur.fetchall())
```

## Notes

- The package is intentionally a small overlay, not a fork of `snowflake-connector-python`.
- The wrapper keeps the normal Snowflake CLI UX but changes request authentication headers before HTTP requests are sent.
- Local Embucket/Rustice deployments do not need this patch; they can use the standard Snowflake CLI connection directly.
