#!/usr/bin/env bash
# One-shot setup on an Oracle Cloud always-free ARM instance (Ubuntu 22.04/24.04).
# Idempotent — safe to re-run.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/jobpipe}"

echo "==> Packages"
sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl git

echo "==> Docker"
if ! command -v docker >/dev/null 2>&1; then
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker "$USER"
  echo "    Added $USER to the docker group. Log out and back in for it to take effect."
fi

echo "==> App directory"
mkdir -p "$APP_DIR"/{data,out}
cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "    !! No .env found in $APP_DIR"
  echo "    Copy .env.example to .env and fill it in, then re-run."
  exit 1
fi
chmod 600 .env

echo "==> Firewall"
# The dashboard binds to 127.0.0.1 only. Cloudflare Tunnel reaches it from inside
# the host, so no ingress rule and no open port is required. Nothing to do here —
# this block is a deliberate no-op, documented so you don't add one by reflex.
echo "    Dashboard is loopback-only by design. No ingress rule needed."

echo "==> Build and start"
docker compose build
docker compose up -d

echo "==> State"
docker compose ps
echo
echo "Next:"
echo "  1. docker compose exec review python -m jobpipe.cli status"
echo "  2. Point your existing Cloudflare Tunnel at http://localhost:8080"
echo "  3. Put the tunnel hostname behind Cloudflare Access (your email only)"
