#!/usr/bin/env bash
# End-to-end setup: install Docker (if needed), then bring up the stack
# (Ollama, Kafka, Kafka UI, code-server, Portainer) via docker compose.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

sudo "${SCRIPT_DIR}/install-docker.sh"

cd "${REPO_ROOT}"
docker compose pull
docker compose up -d

echo
echo "Portainer:  https://<server-ip>:9443"
echo "Kafka UI:   http://<server-ip>:8085"
echo "code-server: http://<server-ip>:8443"
echo "Ollama:     http://<server-ip>:11434"
