#!/usr/bin/env bash
# Install Docker Engine + Compose plugin on Ubuntu/Debian.
# Idempotent: safe to re-run on a host that already has Docker installed.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this script as root (e.g. sudo $0)" >&2
  exit 1
fi

if command -v docker >/dev/null 2>&1; then
  echo "Docker is already installed: $(docker --version)"
else
  echo "Installing Docker Engine..."
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings

  # shellcheck disable=SC1091
  . /etc/os-release
  DISTRO_ID="${ID}"

  curl -fsSL "https://download.docker.com/linux/${DISTRO_ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc

  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${DISTRO_ID} ${VERSION_CODENAME} stable" \
    | tee /etc/apt/sources.list.d/docker.list > /dev/null

  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl enable --now docker

TARGET_USER="${SUDO_USER:-$USER}"
if [ "${TARGET_USER}" != "root" ] && ! id -nG "${TARGET_USER}" | grep -qw docker; then
  usermod -aG docker "${TARGET_USER}"
  echo "Added ${TARGET_USER} to the docker group. Log out and back in for it to take effect."
fi

docker --version
docker compose version
