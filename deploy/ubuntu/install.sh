#!/usr/bin/env sh
set -eu

DEPLOY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$DEPLOY_DIR"

command -v docker >/dev/null 2>&1 || {
  echo "Docker Engine with Compose v2 is required." >&2
  exit 1
}
command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required to generate credentials." >&2
  exit 1
}

if [ "$(id -u)" -eq 0 ]; then
  privilege=
elif command -v sudo >/dev/null 2>&1; then
  privilege=sudo
else
  echo "Root or sudo is required to prepare /var/lib/recall." >&2
  exit 1
fi

$privilege install -d -m 755 /var/lib/recall
$privilege install -d -m 750 -o 10001 -g 10001 /var/lib/recall/data
$privilege install -d -m 750 -o 0 -g 0 /var/lib/recall/ollama
$privilege install -d -m 750 -o 1000 -g 1000 /var/lib/recall/nats

# Runtime databases, backups, models, and JetStream files now have stable
# host paths. Configuration and bootstrap secrets remain beside Compose.

install -d -m 700 secrets
if [ ! -f secrets/api-token ]; then
  openssl rand -base64 48 > secrets/api-token
  chmod 600 secrets/api-token
fi
if [ ! -f recall.env ]; then
  recall_password=$(openssl rand -hex 32)
  prism_password=$(openssl rand -hex 32)
  sed \
    -e "s/replace-with-a-random-password/$recall_password/" \
    -e "s/replace-with-a-different-random-password/$prism_password/" \
    recall.env.template > recall.env
  chmod 600 recall.env
fi

docker compose pull
docker compose build --pull recall
docker compose up -d
docker compose exec ollama ollama pull embeddinggemma
docker compose exec ollama ollama pull nemotron-3-nano:4b
docker compose exec recall recall-admin migrate
docker compose exec recall recall-admin nats bootstrap

echo "Recall is listening on 127.0.0.1:8788."
if command -v tailscale >/dev/null 2>&1; then
  tailscale serve --bg http://127.0.0.1:8788
  echo "Tailscale Serve now publishes Recall privately."
else
  echo "Install Tailscale, sign in, then run:"
  echo "tailscale serve --bg http://127.0.0.1:8788"
fi
