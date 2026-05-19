# Embucket Snowflake Connector Patch

This package adapts `snowflake-connector-python` and Snowflake CLI for Embucket/Rustice running behind Snowpark Container Services (SPCS) public ingress.

SPCS public ingress consumes the standard Snowflake `Authorization` header. This package keeps the Snowflake ingress token in that header for every request:

```http
Authorization: Snowflake Token="<spcs-token>"
```

When Rustice is deployed with `AUTH_TRUST_SPCS_INGRESS=true`, it derives its internal session from Snowflake's SPCS caller context headers (`Sf-Context-*`). The Embucket/Rustice token returned by `/session/v1/login-request` is only an internal server-side session id in this mode and is not sent back as client authentication.

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

## Snowflake CLI Wrapper

If Rustice was deployed with `deploy/spcs/deploy.sh`, the deploy script creates a ready-to-use Snowflake CLI config:

```bash
embucket-snow --config-file /path/to/rustice/deploy/spcs/generated/config.toml \
  sql -c embucket_spcs \
  -q "SELECT * FROM embucket.public.smoke"
```

The generated `embucket_spcs` profile points to the SPCS ingress endpoint and tells the wrapper which regular Snowflake CLI profile to use for ingress authentication. On each process start, the wrapper logs in to Snowflake through that source profile, issues a short-lived SPCS session token with the Python connector, keeps it in memory, and sends it as the standard SPCS ingress `Authorization` header. No daily token file is required for this path.

To configure a profile manually, create a normal Snowflake CLI connection that points to the SPCS public ingress host:

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
spcs_token_connection = "snowflake"
spcs_token_config_file = "/path/to/.snowflake/config.toml"
```

Get the ingress host with:

```bash
snow --config-file /path/to/config.toml sql -c snowflake \
  -q "SHOW ENDPOINTS IN SERVICE RUSTICE_APP.PUBLIC.RUSTICE_SERVICE"
```

## SPCS Ingress Token

SPCS public ingress requires a Snowflake token on every request. This is the only client-side auth token used by this package.

The default path is automatic token issuance from a regular Snowflake CLI profile:

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
spcs_token_connection = "snowflake"
spcs_token_config_file = "/path/to/.snowflake/config.toml"
```

`spcs_token_connection` must name a normal Snowflake connection whose user/role has access to the service endpoint. The wrapper uses that connection only to issue an ingress token, then sends SQL requests directly to Embucket/Rustice.

You can also set the source profile with environment variables:

```bash
export EMBUCKET_SPCS_TOKEN_CONNECTION=snowflake
export EMBUCKET_SPCS_TOKEN_CONFIG_FILE=/path/to/.snowflake/config.toml
```

The Rustice deploy script can also create an ingress service user/PAT as a fallback for non-interactive environments. When doing it manually, create a service user with access only to the Rustice service endpoint:

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

Run Snowflake CLI through the wrapper. Requests go directly to the Embucket/Rustice image behind the SPCS public endpoint, not to a Snowflake virtual warehouse:

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

Token rotation is easiest with `EMBUCKET_SPCS_TOKEN_FILE`: the wrapper reads the file when preparing requests, so an external refresh process or deploy script can replace the file without changing the Snowflake CLI profile. A token passed through `EMBUCKET_SPCS_TOKEN`, `EMBUCKET_SPCS_AUTHORIZATION`, or `spcs_token=` is treated as a static value for that process.

For environments that mint short-lived ingress tokens, provide a token command. It is executed without a shell when a request is prepared, and it can return either the raw token or the full `Snowflake Token="..."` header value:

```bash
export EMBUCKET_SPCS_TOKEN_COMMAND="/path/to/get-spcs-token"
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
    # or: spcs_token_command="/path/to/get-spcs-token",
)

cur = conn.cursor()
cur.execute("SELECT * FROM embucket.public.smoke")
print(cur.fetchall())
```

## Notes

- The package is intentionally a small overlay, not a fork of `snowflake-connector-python`.
- The wrapper keeps the normal Snowflake CLI UX but changes request authentication headers before HTTP requests are sent.
- Local Embucket/Rustice deployments do not need this patch; they can use the standard Snowflake CLI connection directly.
