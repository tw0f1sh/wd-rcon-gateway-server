#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Bitte als root ausführen." >&2; exit 1; fi
APP=/opt/wardogs-gateway
id wardogs >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin wardogs
mkdir -p "$APP" "$APP/data" "$APP/logs"
cp -a . "$APP/"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install -r "$APP/requirements.txt"
[[ -f "$APP/gateway.toml" ]] || cp "$APP/gateway.toml.example" "$APP/gateway.toml"
chown -R wardogs:wardogs "$APP"
chmod 750 "$APP" "$APP/data" "$APP/logs"
cp "$APP/deploy/wardogs-gateway.service" /etc/systemd/system/wardogs-gateway.service
if [[ ! -f /etc/wardogs-gateway.env ]]; then
  cp "$APP/.env.example" /etc/wardogs-gateway.env
  chmod 600 /etc/wardogs-gateway.env
  echo "WICHTIG: /etc/wardogs-gateway.env bearbeiten und Master-RCON-Token setzen."
fi
systemctl daemon-reload
echo "Installation vorbereitet. Danach:"
echo "  nano /etc/wardogs-gateway.env"
echo "  nano $APP/gateway.toml"
echo "  systemctl enable --now wardogs-gateway"
echo "  $APP/.venv/bin/python -m wardogs_gateway --config $APP/gateway.toml key-create --name NAME --role admin"
