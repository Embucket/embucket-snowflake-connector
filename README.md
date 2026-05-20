# Embucket Snowflake Connector Patch

This package adapts `snowflake-connector-python` and Snowflake CLI for Embucket/Rustice running behind Snowpark Container Services (SPCS) public ingress.

SPCS public ingress authenticates every request with the standard Snowflake header:

```http
Authorization: Snowflake Token="<spcs-token>"
```

`embucket-snow` keeps that Snowflake ingress token in `Authorization` on login, query, and result requests. It does not send `X-Embucket-Authorization`. When Rustice is deployed with `AUTH_TRUST_SPCS_INGRESS=true`, Rustice treats successful SPCS ingress as the authentication boundary and derives its internal session from Snowflake caller context headers (`Sf-Context-*`).

The token returned by Rustice `/session/v1/login-request` is only an opaque server-side session id in this mode; it is not a client authentication token.

## Install

```bash
python -m pip install "embucket-snowflake-connector[cli] @ git+https://github.com/Embucket/embucket-snowflake-connector.git"
```

For local development:

```bash
git clone https://github.com/Embucket/embucket-snowflake-connector.git
cd embucket-snowflake-connector
python -m pip install -e ".[cli]"
```

## Recommended Workflow

Deploy Rustice to SPCS from the `rustice` repository first. The deploy script writes a ready-to-use config at `deploy/spcs/generated/config.toml`:

```bash
SNOW_CONFIG_FILE=/path/to/.snowflake/config.toml \
SNOW_CONNECTION=snowflake \
RUSTICE_HORIZON_DATABASE=RUSTICE_E2E \
RUSTICE_HORIZON_ROLE=RUSTICE_E2E_ROLE \
RUSTICE_GRANT_TO_ROLE=<role-used-by-snowflake-profile> \
RUSTICE_HORIZON_TABLES=PUBLIC.SMOKE \
RUSTICE_IMAGE_TAG=latest \
./deploy/spcs/deploy.sh
```

Then query the Rustice service through SPCS:

```bash
embucket-snow --config-file /path/to/rustice/deploy/spcs/generated/config.toml \
  sql -c embucket_spcs \
  -q "SELECT * FROM embucket.public.smoke ORDER BY id"
```

The generated `embucket_spcs` profile points to the SPCS public ingress host and includes:

```toml
spcs_token_connection = "snowflake"
spcs_token_config_file = "/path/to/.snowflake/config.toml"
```

That source Snowflake profile is used only to issue short-lived SPCS ingress tokens. `embucket-snow` keeps the token in memory and sends SQL directly to the Embucket/Rustice service. No daily token file is needed for this path.

The role used by `spcs_token_connection` must have access to the SPCS service endpoint. The Rustice deploy script grants that when `RUSTICE_GRANT_TO_ROLE=<role-used-by-snowflake-profile>` is set.

## Manual CLI Config

If you deploy SPCS manually or with `deploy/spcs/deploy.sql`, get the ingress host:

```bash
snow --config-file /path/to/.snowflake/config.toml sql -c snowflake \
  -q "SHOW ENDPOINTS IN SERVICE RUSTICE_APP.PUBLIC.RUSTICE_SERVICE"
```

Grant the service endpoint to the role used by the source Snowflake profile:

```sql
GRANT USAGE ON DATABASE RUSTICE_APP TO ROLE <role-used-by-snowflake-profile>;
GRANT USAGE ON SCHEMA RUSTICE_APP.PUBLIC TO ROLE <role-used-by-snowflake-profile>;
GRANT SERVICE ROLE RUSTICE_APP.PUBLIC.RUSTICE_SERVICE!RUSTICE_USER
  TO ROLE <role-used-by-snowflake-profile>;
```

Create a config:

```toml
default_connection_name = "embucket_spcs"

[connections.embucket_spcs]
host = "<ingress_url from SHOW ENDPOINTS, without https://>"
protocol = "https"
port = 443
account = "embucket"
user = "embucket"
password = "embucket"
database = "embucket"
schema = "public"
warehouse = "embucket"
spcs_token_connection = "snowflake"
spcs_token_config_file = "/path/to/.snowflake/config.toml"
```

The `account`, `user`, `password`, `database`, `schema`, and `warehouse` values are compatibility values for the Snowflake-compatible client surface. In trusted SPCS mode, ingress authentication comes from `spcs_token_connection`, not from the placeholder password.

Run a smoke query:

```bash
embucket-snow --config-file /path/to/embucket-spcs-config.toml \
  sql -c embucket_spcs \
  -q "SELECT * FROM embucket.public.smoke ORDER BY id"
```

## Token Sources

The recommended token source is a regular Snowflake profile configured with `spcs_token_connection`. It lets `embucket-snow` issue short-lived SPCS ingress tokens automatically and keep them in memory for the CLI process.

You can configure that source in TOML:

```toml
spcs_token_connection = "snowflake"
spcs_token_config_file = "/path/to/.snowflake/config.toml"
```

or with environment variables:

```bash
export EMBUCKET_SPCS_TOKEN_CONNECTION=snowflake
export EMBUCKET_SPCS_TOKEN_CONFIG_FILE=/path/to/.snowflake/config.toml
```

Fallback token sources are supported for non-interactive or custom token-management environments:

```bash
export EMBUCKET_SPCS_TOKEN="<pat-or-oauth-token-for-spcs-ingress>"
export EMBUCKET_SPCS_AUTHORIZATION='Snowflake Token="<pat-or-oauth-token-for-spcs-ingress>"'
export EMBUCKET_SPCS_TOKEN_FILE=/path/to/embucket_spcs_token
export EMBUCKET_SPCS_TOKEN_COMMAND="/path/to/get-spcs-token"
```

`EMBUCKET_SPCS_TOKEN_COMMAND` is executed without a shell when a request is prepared. It may return either a raw token or a full `Snowflake Token="..."` header value.

The Rustice deploy script can create an ingress service-user PAT as a fallback. If you create it manually, keep the role scoped to the service endpoint:

```sql
CREATE ROLE IF NOT EXISTS RUSTICE_INGRESS_ROLE;

CREATE USER IF NOT EXISTS RUSTICE_INGRESS_SVC
  TYPE = SERVICE
  DEFAULT_ROLE = RUSTICE_INGRESS_ROLE;

GRANT ROLE RUSTICE_INGRESS_ROLE TO USER RUSTICE_INGRESS_SVC;
GRANT USAGE ON DATABASE RUSTICE_APP TO ROLE RUSTICE_INGRESS_ROLE;
GRANT USAGE ON SCHEMA RUSTICE_APP.PUBLIC TO ROLE RUSTICE_INGRESS_ROLE;
GRANT SERVICE ROLE RUSTICE_APP.PUBLIC.RUSTICE_SERVICE!RUSTICE_USER
  TO ROLE RUSTICE_INGRESS_ROLE;

CREATE AUTHENTICATION POLICY IF NOT EXISTS RUSTICE_APP.PUBLIC.RUSTICE_INGRESS_PAT_AUTH_POLICY
  PAT_POLICY = (
    NETWORK_POLICY_EVALUATION = ENFORCED_NOT_REQUIRED
    REQUIRE_ROLE_RESTRICTION_FOR_SERVICE_USERS = TRUE
  );

ALTER USER IF EXISTS RUSTICE_INGRESS_SVC
  SET AUTHENTICATION POLICY RUSTICE_APP.PUBLIC.RUSTICE_INGRESS_PAT_AUTH_POLICY FORCE;

ALTER USER IF EXISTS RUSTICE_INGRESS_SVC
  ADD PROGRAMMATIC ACCESS TOKEN RUSTICE_INGRESS_PAT
  ROLE_RESTRICTION = 'RUSTICE_INGRESS_ROLE'
  DAYS_TO_EXPIRY = 1;
```

Copy the returned `token_secret` into a local token file:

```bash
umask 077
printf '%s' '<token_secret>' > /path/to/embucket_spcs_token
```

## Python Connector

Use `connect()` when embedding the same behavior in Python. The preferred path is the same automatic token issuance from a regular Snowflake profile:

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
    spcs_token_connection="snowflake",
    spcs_token_config_file="/path/to/.snowflake/config.toml",
)

cur = conn.cursor()
cur.execute("SELECT * FROM embucket.public.smoke ORDER BY id")
print(cur.fetchall())
```

Static token sources are also available:

```python
conn = connect(
    host="<ingress-host-without-https>",
    protocol="https",
    port=443,
    account="embucket",
    user="embucket",
    password="embucket",
    spcs_token="<pat-or-oauth-token-for-spcs-ingress>",
    # or: spcs_token_file="/path/to/embucket_spcs_token",
    # or: spcs_token_command="/path/to/get-spcs-token",
)
```

## Notes

- The package is intentionally a small overlay, not a fork of `snowflake-connector-python`.
- The wrapper keeps the normal Snowflake CLI UX but changes request authentication headers before HTTP requests are sent.
- Requests go directly to the Embucket/Rustice image behind the SPCS public endpoint, not to a Snowflake virtual warehouse.
- Local Embucket/Rustice deployments do not need this patch; they can use the standard Snowflake CLI connection directly.
