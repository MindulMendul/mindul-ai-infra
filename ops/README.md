# 운영·백업·복원

운영 대시보드: https://mindul-server.duckdns.org/grafana/d/mindul-ops

현재 설정은 **로컬 암호화 백업과 Grafana 내부 경보**입니다. 외부 저장소와
이메일·메신저 수신처는 연결하지 않았습니다. 같은 서버의 백업은 서버 전체
유실에 대비한 외부 사본이 아니므로, 외부 저장소가 정해지면 암호화 파일과
인증 manifest를 복사하고 키는 별도 위치에 보관해야 합니다.

## Git으로 관리하는 범위

- `docker-compose.yml`: 공통 서비스, 데이터 볼륨, 네트워크, 실행 이미지 digest
- `monitoring/`: 메트릭·RUM·트레이스 수집과 Grafana 대시보드·경보
- `scripts/mindul.py`, `deployments/`: 앱 배포·상태·롤백 도구
- `ops/`: 백업·복원·정기 정리·HTTPS 점검과 설치 정책
- `.github/workflows/deploy.yml`: 검사에 통과한 정확한 커밋의 SSH 배포

비밀 `.env`, 암호화 키, 백업 파일, 실행 중인 앱 등록 정보와 데이터 볼륨은
Git에 넣지 않습니다. Git은 정의를, 백업은 복구에 필요한 상태와 데이터를 보관합니다.

## CI/CD

`main` push 시 Compose 검사, Python 테스트, ShellCheck, 수집기 회귀 테스트를
실행한 뒤 그 커밋 SHA를 서버에 전달합니다. 서버는 dirty 작업 트리를 덮어쓰지
않고 fast-forward만 허용합니다. 더 최신 커밋이 이미 있으면 오래된 배포를 건너뜁니다.
Git 갱신 후에는 새 커밋의 배포 코드를 다시 실행합니다.

기존 `.env`와 Grafana 비밀번호는 유지합니다. 서버에 파일이 없을 때만 처음
비밀값을 생성합니다. 공통 인프라와 모니터링을 함께 적용하며 Caddy 설정을
검증하고 HTTPS를 확인합니다. 파일 bind mount의 이전 inode가 남지 않도록
설정 반영 시 필요한 컨테이너를 재생성합니다. 짧은 재접속이 발생할 수 있습니다.

SSH 키는 기존 GitHub secret `SSH_PRIVATE_KEY`의 base64 형식을 사용합니다.
`SSH_HOST`, `SSH_USER`, `SSH_PORT`, `DEPLOY_PATH`도 기존 secret을 사용합니다.
CI 임시 개인키 파일은 0600이고 작업 종료 때 제거합니다. 서버 호스트 키는
공개키 파일 `ops/ssh_known_hosts`와 일치해야 합니다. 서버 교체로 호스트 키가
바뀌면 새 공개키를 확인한 뒤 이 파일을 갱신해야 합니다.

현재 이미지는 이 ARM64 서버에서 실제 실행한 digest로 고정했습니다.
`ops/images.lock.json`은 고정한 이미지 목록입니다. 업데이트는 새 버전을 검증한 뒤
Compose의 digest와 이 목록을 함께 바꾸는 방식으로 수행합니다.

```sh
# 새 서버 초기 설치: Ubuntu 사용자로 실행 (Docker 설치에 sudo 사용)
scripts/bootstrap.sh

# 현재 체크아웃의 정의 수동 적용
mindul-ops deploy

# 검증한 특정 커밋만 fast-forward 배포
mindul-ops deploy --commit <40자리-커밋-SHA>
```

웹 저장소의 최신 `main`은 서버 초기 체크아웃 이후 별도로 진행됐습니다.
현재 실행 중인 소스는 다음 보존 브랜치로 GitHub에 저장했습니다.

- 운영: `MindulMendul/murder-parker`, `ops/live-rum-snapshot-20261105`
- QA: `MindulMendul/murder-parker`, `ops/qa-rum-snapshot-20261105`

기존 웹 컨테이너를 최신 main으로 자동 전환하지는 않습니다. 현재 운영본의
Git 보존과 이후 main 통합·앱 배포는 별개입니다.

## 영구 저장

Kafka는 기존 컨테이너의 `/tmp/kraft-combined-logs`에서 데이터와 소유권을
복사한 뒤 전용 `kafka_data` 볼륨의 `/var/lib/kafka/data`로 이관했습니다.
클러스터 ID는 기존 `meta.properties`의 값과 같습니다.
코드 서버의 `/config`도 `vscode_config` 볼륨으로 이관했고, 작업 폴더
`/config/workspace`의 `/home/ubuntu` bind mount는 유지합니다.

기존 데이터가 있는 서버에서 빈 볼륨으로 덮어쓰지 않습니다. 이관 대상 볼륨이
예상과 다르게 먼저 존재하면 자동으로 덮어쓰는 대신 중단합니다.
운영·QA 게임 DB, Grafana, Portainer, Caddy 데이터도 각각의 기존 볼륨을 유지합니다.

## 예약 작업

| 작업 | 주기 (한국 시간) | systemd unit |
|---|---|---|
| 암호화 백업 | 매일 03:20 | `mindul-backup.timer` |
| 별도 볼륨 복원 검증 | 일요일 04:00 | `mindul-restore-check.timer` |
| 빌드 캐시·오래된 dangling 이미지 정리 | 일요일 04:30 | `mindul-maintenance.timer` |
| HTTPS·TLS·컨테이너 점검 | 5분마다 | `mindul-health.timer` |

최대 30초의 지연 분산이 있습니다. 백업·배포·유지보수는 잠금으로 겹치지 않으며
앱 배포 도중의 일관되지 않은 등록 정보를 백업하지 않도록 앱 배포 잠금도 사용합니다.
첫 설치에서는 첫 백업과 복원 검증을 바로 수행합니다.

```sh
systemctl list-timers 'mindul-*'
journalctl -u mindul-backup.service --since today
journalctl -u mindul-restore-check.service --since today
mindul-ops status
```

## 백업 범위와 보호

백업 파일: `~/.local/share/mindul-backups/mindul-*.tar.gz.enc`

인증 manifest: 같은 이름의 `.tar.gz.json`

키 파일: **`~/.config/mindul/backup.key`** (Git·백업 archive에 포함하지 않음)

- AES-256-CBC + PBKDF2로 암호화하고 별도 파생 키의 HMAC-SHA256으로
  암호문을 인증합니다. 변조된 파일은 복호화 전에 거부합니다.
- 키·비밀 설정은 mode 0600, 상태·백업 디렉터리는 0700입니다.
- 마지막 성공 백업 3개를 보관합니다. 기본 평문 snapshot 한도는 2GiB입니다.
- 게임 운영·QA DB, Grafana DB는 SQLite online backup API를 사용합니다.
  WAL에 커밋된 데이터까지 포함하고 WAL 파일을 단순 복사하지 않습니다.
- Portainer와 Kafka는 일관된 복사를 위해 잠시 정지한 뒤 반드시 다시 시작합니다.
  웹 게임 서버와 Ollama는 백업 때문에 정지하지 않습니다.
- Caddy 인증서·설정, Portainer, Kafka, 코드 서버 설정, 앱 등록 상태,
  배포 snapshot, 비밀 환경변수와 Ollama 설정·모델 manifest를 포함합니다.
- 새 `mindul` 앱의 named volume도 등록 정보에서 발견합니다. 종류를 모르는
  볼륨은 writer를 잠시 정지하는 cold backup으로 처리합니다.
- 재다운로드 가능한 Ollama 모델 blob, Prometheus/Loki/Tempo 시계열·로그·트레이스
  이력은 기본 백업에서 제외합니다. 대시보드와 경보 정의는 Git에 있습니다.
- 절대 symlink는 그대로 따라 읽지 않고 링크 대상으로 기록해 복원합니다.

**키 파일을 잃으면 암호화된 백업을 복원할 수 없습니다.** 외부 저장소와 함께
사용할 때는 키를 백업 파일과 별도로 관리하세요. 현재 외부 사본은 없습니다.

```sh
mindul backup                    # 직접 백업
mindul-ops verify                # 최신 archive 인증 + SQLite 무결성/행 수 확인
mindul restore-check             # 새 테스트 볼륨에 실제 복원 후 검증·정리
mindul-ops verify /경로/백업.tar.gz.enc
```

## 복원

실행 중인 서비스에 자동 덮어쓰지 않습니다. 먼저 빈 디렉터리로 복호화·검증합니다.

```sh
mindul-ops restore /경로/백업.tar.gz.enc --directory /안전한/빈-복원-디렉터리
```

복원 디렉터리에는 다음 내용이 있습니다.

- `config/infra/`: 인프라 정의와 비밀 `.env`
- `config/caddy-routes/`: 자동 생성 앱 경로
- `config/app-state/`: 앱 등록 정보와 최근 배포 snapshot
- `config/murder-parker*/`: Git 밖의 웹 환경변수
- `repositories.json`: 실행 소스의 Git 브랜치·커밋
- `volumes/<이름>/data`, `owners.json`, `links.json`, `sqlite.json`: 데이터와
  원래 소유권·링크·검증용 테이블 행 수

서버 교체 시에는 Git에서 정의를 확보하고 Docker를 설치한 뒤, 서비스를
시작하기 **전에** 비밀 설정·앱 상태·볼륨을 복원하세요. 비밀 파일은 `install -m 600`으로
옮깁니다. 새 볼륨으로 복원할 때 사용하는 원리는 아래와 같습니다.

```sh
# 반드시 새 빈 볼륨 사용. 실행 중인 운영 볼륨에 적용하지 마세요.
docker volume create recovered-game-data
docker run --rm --network none --user 0 \
  -v recovered-game-data:/target \
  -v /복원/volumes/deploy_festival-data:/snapshot:ro \
  -v "$PWD/ops/volume_snapshot.py:/tools/snapshot.py:ro" \
  mindul-monitoring-telemetry-gateway:latest python /tools/snapshot.py restore
```

helper 이미지는 `docker compose --env-file monitoring/.env -f monitoring/compose.yaml build telemetry-gateway`로
미리 만들 수 있습니다. 복원 helper는 비어 있지 않은 대상 볼륨을 거부하며,
파일 UID/GID·모드를 복원하고 SQLite 무결성과 행 수를 다시 검사합니다.
복원한 새 볼륨을 Compose에 연결한 다음 앱 상태와 HTTPS를 확인하세요.
이미지·모델 가중치는 별도로 재빌드·다운로드해야 하며 기존 private Git/registry
접근 자격도 새 서버에 설정해야 합니다. 현재는 전체 서버의 원클릭 복구까지
구현한 것은 아닙니다.

## 경보와 정리 정책

Grafana의 **Mindul Operations** 폴더에 다음 13개 경보가 파일로 등록됩니다.

- 디스크 85% 경고 / 95% 위험, 메모리·CPU 과다
- Prometheus 수집 실패, 필수 컨테이너 중단
- 공개 HTTPS 실패, TLS 14일 이내 만료, 점검 결과 지연
- 36시간 백업 없음, 마지막 백업 실패, 8일 복원 검증 없음, 8일 정리 없음

기본 알림 수신처는 같은 Docker 네트워크의 내부 수신기입니다. 외부 이메일이나
메신저로 발송하지 않습니다. Grafana에서 상태를 확인하고 내부 수신 내역은
`mindul_alert_notifications_total`과 수집기 로그에 남습니다.
실제 수신처가 정해지면 별도 contact point를 추가할 수 있습니다.

정리는 사용하지 않는 Docker 빌드 캐시를 1GB 목표로 제한하고, 최근 성공 백업이
있는 경우 14일 넘은 dangling 이미지만 제거합니다. 실행 중인 이미지와 태그가
붙은 앱 이미지, 운영 데이터 볼륨은 삭제하지 않습니다. 앱별 최근 이미지 두 개
보관은 기존 `mindul` 배포 도구가 담당합니다. Docker가 공유 중인 레이어 때문에
전체 디스크 사용량이 정확히 1GB로 제한되는 것은 아닙니다.

```sh
mindul maintenance
mindul health
```

## 검증

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

암호화 round-trip, HMAC 변조 거부, manifest 일치, WAL 포함 SQLite online backup,
빈 대상 복원, symlink 보존, 크기 한도와 설정 검사를 포함합니다. 첫 실제 백업은
운영·QA·Grafana DB 3개를 격리된 Docker 볼륨으로 복원해 검증했습니다.
