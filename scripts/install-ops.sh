#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ "${MINDUL_OPS_INITIALIZED:-0}" != "1" ]]; then
  python3 "${REPO_ROOT}/ops/mindul_ops.py" init
fi
mkdir -p "${HOME}/.local/bin"
ln -sfn "${REPO_ROOT}/ops/mindul_ops.py" "${HOME}/.local/bin/mindul-ops"
chmod +x "${REPO_ROOT}/ops/mindul_ops.py"
UNIT_STAGE="$(mktemp -d)"
trap 'rm -rf "${UNIT_STAGE}"' EXIT
python3 - "${REPO_ROOT}" "${UNIT_STAGE}" "$(id -un)" <<'PY'
from pathlib import Path
import sys
root,stage,user=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
jobs={
 'backup':('daily encrypted local backup','*-*-* 03:20:00 Asia/Seoul'),
 'restore-check':('weekly isolated-volume restore verification','Sun *-*-* 04:00:00 Asia/Seoul'),
 'maintenance':('weekly bounded Docker cache cleanup','Sun *-*-* 04:30:00 Asia/Seoul'),
 'health':('HTTPS/TLS and container health probes',None),
}
for command,(description,calendar) in jobs.items():
 unit='mindul-'+command
 service=f'''[Unit]
Description=Mindul {description}
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User={user}
SupplementaryGroups=docker
WorkingDirectory={root}
ExecStart=/usr/bin/python3 {root}/ops/mindul_ops.py {command}
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=30min
TimeoutStopSec=60s
KillMode=mixed
UMask=0077
'''
 timer=f'''[Unit]
Description=Mindul {description} schedule

[Timer]
'''
 timer+=f'OnCalendar={calendar}\nPersistent=true\n' if calendar else 'OnBootSec=2min\nOnUnitActiveSec=5min\n'
 timer+='RandomizedDelaySec=30\n\n[Install]\nWantedBy=timers.target\n'
 (stage/(unit+'.service')).write_text(service)
 (stage/(unit+'.timer')).write_text(timer)
PY
sudo install -m 0644 "${UNIT_STAGE}"/*.service "${UNIT_STAGE}"/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mindul-backup.timer mindul-restore-check.timer mindul-maintenance.timer mindul-health.timer
echo "운영 타이머 설치 완료. 백업 키는 ~/.config/mindul/backup.key에 보관합니다."
