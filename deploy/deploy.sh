#!/usr/bin/env bash
# Ship the working tree to the EC2 instance from deploy/provision.py and (re)configure it.
#
#   bash deploy/deploy.sh
#
# Server layout: /opt/acc-poc (code, .venv, .env), systemd unit acc-poc (uvicorn on 127.0.0.1:8080),
# Caddy on :80/:443 with an automatic Let's Encrypt cert for <ip>.sslip.io.
# Everything is behind HTTP basic auth except the A2A endpoints (own bearer key) and /healthz.
# Secrets live in deploy/.secrets (gitignored): BASIC_USER, BASIC_PASS, A2A_KEY.
set -euo pipefail
cd "$(dirname "$0")/.."

STATE=deploy/.state.json
[ -f "$STATE" ] || { echo "run deploy/provision.py first"; exit 1; }
IP=$(python3 -c "import json;print(json.load(open('$STATE'))['ip'])")
HOST=$(python3 -c "import json;print(json.load(open('$STATE'))['host'])")
KEY=$(python3 -c "import json;print(json.load(open('$STATE'))['key_path'])")
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 ubuntu@$IP"

if [ ! -f deploy/.secrets ]; then
  umask 077
  cat > deploy/.secrets <<EOF
BASIC_USER=demo
BASIC_PASS=$(python3 -c "import secrets;print(secrets.token_urlsafe(12))")
A2A_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
EOF
fi
# shellcheck disable=SC1091
source deploy/.secrets

echo "== waiting for ssh on $IP"
for _ in $(seq 1 40); do $SSH true 2>/dev/null && break; sleep 5; done

echo "== packing working tree"
TMP=$(mktemp -d)
{ git ls-files -co --exclude-standard | grep -v -E '^guide-.*\.png|Zone\.Identifier$|^deploy/\.'; \
  find results/raw .cache -type f 2>/dev/null; } | sort -u > "$TMP/files"
tar czf "$TMP/app.tgz" -T "$TMP/files"
# server .env = local .env (OpenRouter key etc.) + deployment overrides
{ grep -v -E '^(A2A_API_KEY|ALLOW_TEST_RUNNER|VOICE_MODE|LLM_MODE)=' .env || true
  echo "LLM_MODE=live"; echo "VOICE_MODE=aws"; echo "ALLOW_TEST_RUNNER=false"; echo "A2A_API_KEY=$A2A_KEY"; } > "$TMP/env"
scp -q -i "$KEY" "$TMP/app.tgz" "$TMP/env" "ubuntu@$IP:/tmp/"
rm -rf "$TMP"

echo "== configuring server"
$SSH "BASIC_USER='$BASIC_USER' BASIC_PASS='$BASIC_PASS' HOST='$HOST' bash -s" <<'REMOTE'
set -euo pipefail
if ! command -v caddy >/dev/null; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gpg >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy >/dev/null
fi
if [ ! -f /swapfile ]; then  # t3.micro has 1 GB RAM
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap -q /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
if [ ! -x "$HOME/.local/bin/uv" ]; then curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null; fi

sudo mkdir -p /opt/acc-poc && sudo chown ubuntu:ubuntu /opt/acc-poc
cd /opt/acc-poc
find . -mindepth 1 -maxdepth 1 ! -name .venv ! -name .cache ! -name results -exec rm -rf {} +
tar xzf /tmp/app.tgz
install -m 600 /tmp/env .env
rm -f /tmp/app.tgz /tmp/env
"$HOME/.local/bin/uv" sync --no-dev --frozen -q

sudo tee /etc/systemd/system/acc-poc.service >/dev/null <<UNIT
[Unit]
Description=acc-poc voice agent
After=network-online.target
[Service]
User=ubuntu
WorkingDirectory=/opt/acc-poc
ExecStart=/opt/acc-poc/.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8080 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
UNIT

HASH=$(caddy hash-password --plaintext "$BASIC_PASS")
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
$HOST {
	encode gzip
	@open path /a2a /a2a/* /.well-known/* /healthz
	handle @open {
		reverse_proxy 127.0.0.1:8080
	}
	handle {
		basic_auth {
			$BASIC_USER $HASH
		}
		reverse_proxy 127.0.0.1:8080
	}
}
CADDY
sudo systemctl daemon-reload
sudo systemctl enable -q --now acc-poc
sudo systemctl restart acc-poc
sudo systemctl reload caddy || sudo systemctl restart caddy
for _ in $(seq 1 30); do curl -sf 127.0.0.1:8080/healthz >/dev/null && break; sleep 1; done
curl -s 127.0.0.1:8080/healthz; echo
REMOTE

echo "== https://$HOST/voice  (user: $BASIC_USER, password in deploy/.secrets)"
