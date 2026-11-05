#!/usr/bin/env bash
# Install the standard-library CLI and attach its route directory to existing Caddy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLI_PATH="${HOME}/.local/bin/mindul"

command -v python3 >/dev/null
command -v git >/dev/null
command -v docker >/dev/null
mkdir -p "${HOME}/.local/bin" "${REPO_ROOT}/deployments/caddy"
if [[ -e "${CLI_PATH}" || -L "${CLI_PATH}" ]]; then
  if [[ "$(readlink -f "${CLI_PATH}")" != "${SCRIPT_DIR}/mindul.py" ]]; then
    echo "이미 다른 mindul 명령어가 있습니다: ${CLI_PATH}" >&2
    exit 1
  fi
fi
chmod +x "${SCRIPT_DIR}/mindul.py"
ln -sfn "${SCRIPT_DIR}/mindul.py" "${CLI_PATH}"

cd "${REPO_ROOT}"
HOST_CADDY_SHA="$(sha256sum Caddyfile)"
LIVE_CADDY_SHA="$(docker exec mindul-caddy sha256sum /etc/caddy/Caddyfile 2>/dev/null || true)"
if [[ "${HOST_CADDY_SHA%% *}" != "${LIVE_CADDY_SHA%% *}" ]]; then
  docker compose up -d --no-deps --force-recreate caddy
else
  docker compose up -d --no-deps caddy
fi
docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
python3 - "${REPO_ROOT}" <<'PY'
import json
from pathlib import Path
import subprocess
import sys
root = Path(sys.argv[1])
container = json.loads(subprocess.check_output(['docker', 'inspect', 'mindul-caddy']))[0]
assert any(m['Destination'] == '/etc/caddy/apps' and Path(m['Source']) == root / 'deployments/caddy'
           for m in container['Mounts']), 'Caddy route mount is missing'
(root / 'deployments/caddy/.ready').write_text('mindul-deploy-v1\n')
PY
echo "설치 완료: ${CLI_PATH}"
echo "사용법: mindul deploy https://github.com/owner/repository.git"
