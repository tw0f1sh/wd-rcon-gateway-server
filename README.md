# Wardogs RCON Gateway 1.0.0

This gateway sits between the Wardogs Admin Client and the actual Bearer-authenticated RCON service. The client **no longer receives the master RCON password**, but instead uses an API key issued by the Gateway. The Gateway validates the key and its endpoint permissions, then forwards permitted requests to the real RCON service using the master password.

## Yes, its made with AI, if you want to cry about it, dont use it!
- build and tested on -> `v1 • ++Wardogs+Live-CL-501228`
- read `how_to.txt`

---

## Other Repos for this Tool

- [**wd-rcon-gateway-client**](https://github.com/tw0f1sh/wd-rcon-gateway-client)

---

## Roles

### `admin`

For the standard GUI with **OVERVIEW** and **PLAYER ADMINISTRATION**:

- `GET /v1/health`
- `GET /v1/capabilities`
- `GET /v1/status`
- `GET /v1/players`
- `GET /v1/reserved-slots`
- `POST /v1/players/{id}/message`
- `POST /v1/players/{id}/kick`
- `POST /v1/players/{id}/kill`
- `PATCH /v1/players/{id}`
- `POST /v1/bans`

### `full_admin`

All routes under `/v1`. This also enables the Debug pages **ACTIONS** and **SERVER ADMINISTRATION**, as well as future RCON routes, unless a custom allowlist has been configured for the key.

Each individual key can also be assigned its own **exact allowlist**. As soon as a custom allowlist is set, it replaces the role default for that key.

## Security Model

- The master RCON password is **not** stored in SQLite or `gateway.toml`; it is loaded only from the server environment via `WARDOGS_MASTER_RCON_TOKEN`.
- Issued API keys are stored in SQLite only as SHA-256 hashes. The full key is displayed only when it is created or rotated.
- Disabled, deleted, or expired keys stop working immediately.
- `/v1/capabilities` is filtered per key: the GUI user only sees routes that their key is permitted to use.
- Requests and result codes are logged with the key ID/name; secrets are not logged.
- Gateway management is intentionally **local CLI only**. There is no public HTTP route for creating keys.

## Installation on Ubuntu/Debian

```bash
sudo apt update
sudo apt install -y python3 python3-venv nginx
unzip wardogs_rcon_gateway_v1.0.0.zip
cd wardogs_rcon_gateway
sudo ./deploy/install_ubuntu.sh
```

Then enter the master password only here:

```bash
sudo nano /etc/wardogs-gateway.env
```

Example:

```text
WARDOGS_MASTER_RCON_TOKEN=YOUR_MASTER_RCON_PASSWORD
```

Then:

```bash
sudo chmod 600 /etc/wardogs-gateway.env
sudo nano /opt/wardogs-gateway/gateway.toml
sudo systemctl enable --now wardogs-gateway
sudo systemctl status wardogs-gateway
```

## Issuing an API Key

Admin key:

```bash
sudo -u wardogs /opt/wardogs-gateway/.venv/bin/python -m wardogs_gateway \
  --config /opt/wardogs-gateway/gateway.toml \
  key-create --name "Moderator Max" --role admin
```

Full Admin key:

```bash
sudo -u wardogs /opt/wardogs-gateway/.venv/bin/python -m wardogs_gateway \
  --config /opt/wardogs-gateway/gateway.toml \
  key-create --name "Server Owner" --role full_admin
```

The full `wdg_...` key is displayed **only once**. The user enters this key in the Wardogs GUI in the `RCON Password / Gateway API Key` field.

## Managing Keys

```bash
# List
python -m wardogs_gateway --config gateway.toml key-list

# Details without secret
python -m wardogs_gateway --config gateway.toml key-show KEY_ID

# Immediately revoke / re-enable
python -m wardogs_gateway --config gateway.toml key-revoke KEY_ID
python -m wardogs_gateway --config gateway.toml key-enable KEY_ID

# Reissue secret; the old secret becomes invalid afterwards
python -m wardogs_gateway --config gateway.toml key-rotate KEY_ID

# Change role
python -m wardogs_gateway --config gateway.toml key-set-role KEY_ID full_admin

# Restrict the key to exactly two endpoints
python -m wardogs_gateway --config gateway.toml key-set-permissions KEY_ID \
  --allow "GET /v1/status" \
  --allow "GET /v1/players"

# Use the role default again
python -m wardogs_gateway --config gateway.toml key-set-permissions KEY_ID --inherit-role
```

A key can also be restricted when it is created:

```bash
python -m wardogs_gateway --config gateway.toml key-create \
  --name "Overview Only" --role admin \
  --allow "GET /v1/health" \
  --allow "GET /v1/capabilities" \
  --allow "GET /v1/status"
```

Optionally, a key can expire automatically:

```bash
python -m wardogs_gateway --config gateway.toml key-create \
  --name "Temporary Admin" --role admin --expires-days 7
```

## Connecting the GUI

The Gateway continues to expose the RCON API under `/v1`. Therefore, no technical changes to the desktop client are required:

- Previous Base URL: `http://162.120.3.124:9011/v1`
- New Base URL, for example: `https://rcon-gateway.example.com/v1`
- Previous password: Master RCON password
- New password: the issued `wdg_...` API key

`health`, `status`, `players`, `reserved-slots`, player actions, etc. continue to appear to the client exactly like the existing RCON API. A disallowed endpoint returns HTTP `403`.

## HTTPS Is Required

API keys are Bearer tokens. The Gateway should therefore **not** be exposed over the internet without encryption. The included nginx example is intended for TLS.

More importantly, the current RCON target in the example itself runs over `http://...:9011`. This means the **master RCON password still travels unencrypted between the Gateway and RCON** if that connection crosses an untrusted network. If possible:

1. Allow RCON port `9011` through the firewall only for the Gateway's fixed IP address.
2. Even better, connect Gateway and RCON via VPN/WireGuard/Tailscale or a private network.
3. Completely block public direct access to `9011` so that the Gateway cannot be bypassed.

## nginx

`deploy/nginx.conf.example` contains an example configuration. Adjust the domain and certificate paths, then reload nginx. By default, the Python process itself listens only on `127.0.0.1:9012`.

## Health Check

The normal `GET /v1/health` request is forwarded to RCON with authentication. For local Gateway monitoring, the following additional endpoint is available:

```text
GET /_gateway/health
```

This endpoint contains no RCON data and no secrets.

## Rate Limits

All GUI clients appear to RCON as **a single Gateway IP**. Since the available RCON capability reports a limit of 600 requests/minute/IP, the example configuration conservatively sets:

- global: 540/min
- per key: 180/min

Both values can be changed in `gateway.toml` or disabled by setting them to `0`. Normal GUI polling at 3-second intervals remains below these limits.

## Tests

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```
