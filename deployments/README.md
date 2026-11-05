# 작은 배포 도구 `mindul`

Git 저장소를 기존 Docker·Caddy에 배포하는 Python 표준 라이브러리 스크립트입니다.
추가 웹 관리 서버·DB·패키지는 실행하지 않습니다. 기존 Portainer와 Grafana에서
생성된 컨테이너의 상태와 자원 사용량을 확인할 수 있습니다.

## 사용

서버에서 처음 한 번 설치합니다. 현재 서버에는 이미 설치되어 있습니다.

```sh
./scripts/install-mindul.sh
```

이후 **어느 디렉터리에서든** 실행할 수 있습니다.

```sh
mindul deploy https://github.com/owner/my-web.git
```

기본 동작:

1. Git 기본 브랜치를 새 디렉터리에 복제합니다.
2. Dockerfile 또는 Compose 파일을 감지합니다.
3. 앱 전용 이미지를 빌드하고 컨테이너·볼륨·네트워크를 생성합니다.
4. 컨테이너 준비 상태와 내부 HTTP 응답을 확인합니다.
5. Caddy 설정을 검증하고 HTTPS 주소를 연결합니다.
6. 성공한 배포 두 개를 보관하고 그보다 오래된 자체 이미지·소스를 정리합니다.

기본 주소: **`https://my-web.mindul-server.duckdns.org`**.
현재 DuckDNS의 하위 도메인도 서버 IP로 해석되는 것을 확인했습니다.
앱마다 DuckDNS에 새 주소를 등록할 필요가 없습니다. 인증서는 각 호스트별로
Caddy가 발급하며 인증기관의 발급 한도는 그대로 적용됩니다.
다른 서버에서는 `MINDUL_BASE_DOMAIN`을 실제 연결된 기본 도메인으로 지정하세요.

저장소에는 **실행 가능한 Dockerfile 또는 Compose가 필요**합니다.
프레임워크별 Dockerfile을 추측해 생성하거나 소스 코드의 오류를 자동 수정하지는 않습니다.
추가 환경변수가 필수라면 첫 배포에서 `--env-file`로 제공하세요.
이미 빌드한 ARM64 이미지나 ARM64에서 빌드되는 프로젝트를 사용해야 합니다.

```sh
# 환경변수가 필요한 첫 배포
mindul deploy https://github.com/owner/my-web.git --env-file ~/my-web.env

# 같은 앱을 QA 브랜치로 분리: my-web-qa.mindul-server.duckdns.org
mindul deploy https://github.com/owner/my-web.git --environment qa

# 브랜치가 다르면 직접 지정
mindul deploy https://github.com/owner/my-web.git -e qa --branch develop

# 최초 설정을 재사용해 최신 커밋 재배포
mindul redeploy my-web
mindul redeploy my-web -e qa

mindul list
mindul status my-web
mindul logs my-web --follow
mindul check my-web
mindul restart my-web
mindul rollback my-web

# 저장된 환경변수 편집 후 재배포
mindul env my-web
mindul redeploy my-web

# 컨테이너 정지 / 제거. 제거해도 데이터 볼륨은 보존합니다.
mindul stop my-web
mindul remove my-web
```

브랜치는 최초 운영 배포에서 Git 기본 브랜치, 최초 QA 배포에서 `qa`가 기본입니다.
재배포는 저장된 브랜치를 사용합니다. 자동 GitHub webhook은 아직 연결하지 않았으므로
Git push만으로 배포하지 않으며 `mindul redeploy`를 실행해야 합니다.
비공개 저장소는 서버에 이미 설정된 SSH 키나 Git credential을 사용합니다.
토큰·비밀번호가 들어 있는 HTTPS 주소는 받지 않습니다.

## 저장소의 선택 설정 `mindul.json`

표준 파일만으로 설정이 충분하면 만들 필요 없습니다.
별도 Dockerfile, 내부 포트, 상태 검사, 영구 저장소 등이 필요하면 저장소 루트에 둡니다.

```json
{
  "version": 1,
  "dockerfile": "deploy/Dockerfile",
  "context": ".",
  "port": 3000,
  "health_path": "/api/health",
  "volumes": {"data": "/data"},
  "memory": "1g",
  "cpus": 1
}
```

Dockerfile 방식에서 빌드 변수가 필요하면 `build_args` 객체를 추가할 수 있습니다.
빌드 변수는 이미지에 남을 수 있으므로 API 키 등 비밀값을 전달하지 마세요.
런타임에는 `--env-file`로 제공한 값과 자동 생성한 `APP_ORIGIN`, `APP_RELEASE`가 전달됩니다.
`APP_RELEASE`는 배포별로 고유하며 Dockerfile에도 빌드 인자로 전달합니다.

Compose 프로젝트 예시:

```json
{
  "version": 1,
  "compose": "deploy/compose.yaml",
  "service": "web",
  "port": 3000,
  "health_path": "/api/health"
}
```

Compose 환경변수 보간에는 저장된 `.env`를 사용합니다. 각 서비스가 받을 값은
저장소 Compose의 `environment` / `env_file`로 선언하세요.
공개 웹 서비스의 `APP_ORIGIN`, `APP_RELEASE`는 배포 환경에 맞춰 덮어씁니다.
다중 서비스에서 웹 서비스를 판단할 수 없으면 `--service`가 필요합니다.
명령줄 `--compose`, `--dockerfile`, `--context`, `--port`, `--health-path`,
`--memory`, `--cpus`로도 설정할 수 있고 다음 재배포에서 재사용합니다.

자동 감지 순서는 루트 Compose → `deploy/compose.yaml` → 루트 Dockerfile →
`deploy/Dockerfile`입니다. 내부 포트는 명시 설정 → Compose의 target/expose →
Dockerfile의 EXPOSE 순서로 찾습니다. 찾을 수 없으면 포트를 명시해야 합니다.

## 분리·복구·저장

- 운영과 QA는 Compose 프로젝트와 named volume을 각각 사용합니다.
- 모든 서비스의 호스트 `ports`와 고정 `container_name`은 제거합니다.
  외부 HTTP는 Caddy의 443 포트를 통해서만 연결합니다. 웹 서비스만 공유
  `mindul-net`에 연결하고 나머지 서비스는 해당 앱의 내부 네트워크에 둡니다.
- 기본 런타임 제한은 서비스당 CPU 1코어·RAM 1GiB입니다. 저장소에 선언된
  제한은 존중하며 `mindul.json`이나 CLI 옵션으로 기본값을 변경할 수 있습니다.
  이미지 빌드는 이 런타임 제한과 별개로 서버 자원을 사용합니다.
- 배포 전 디스크 여유 2GiB 이상을 확인합니다. 빌드 자체의 최대 용량을
  보장하는 것은 아니므로 Grafana의 디스크 사용량을 함께 확인하세요.
- 외부 볼륨, 쓰기 가능한 host bind mount, host network/PID, privileged,
  profiles가 있는 Compose는 지원하지 않습니다. 기본 local named volume과
  배포용 Compose를 사용하세요. 읽기 전용 설정 파일 bind mount는 지원합니다.
- 교체 배포이므로 짧은 재접속이 발생할 수 있습니다. 실패하면 기존 이미지와
  런타임 설정으로 자동 복구합니다. `rollback`은 직전 성공 배포로 되돌립니다.
  **DB 내용이나 스키마 변경 자체를 되돌리는 기능은 아닙니다.**
- 마지막 두 배포의 소스·자체 이미지를 보관합니다. Docker 공용 빌드 캐시나
  다른 앱의 이미지, 데이터 볼륨을 전역 삭제하지 않습니다.
- `~/.local/share/mindul-deploy/`에는 앱 설정, 비밀 환경변수, 배포 스냅샷이
  있습니다. 이 디렉터리는 mode 0700, 환경변수·스냅샷은 0600으로 보관합니다.
  복원 시에는 앱 데이터 볼륨과 함께 백업해야 합니다.
- 자동 Caddy 설정은 `deployments/caddy/*.caddy`에 저장하고 Git에서 제외합니다.
  원본 저장소와 `mindul.json`이 배포 정의이며 실행 스냅샷은 런타임 상태입니다.
- 기존 수동 배포된 Murder Mystery·QA·AI·Grafana는 자동으로 이 도구에
  등록하거나 이전하지 않습니다. 기존 방식으로 실행 중이며 새 앱만 등록됩니다.
- 컨테이너 자원은 기존 Grafana에서 자동 발견됩니다. 앱별 RUM·API 계측은
  해당 앱의 SDK/메트릭 구현이 있어야 하며 임의의 저장소에 자동 삽입하지 않습니다.

자신이 관리하고 신뢰하는 저장소를 배포하는 도구입니다. 격리된 공용 빌드
플랫폼이나 다른 사람의 임의 코드를 실행하는 서비스는 아닙니다.

## 검증

```sh
python3 -m unittest discover -s tests -p 'test_mindul.py' -v
```

실제 Docker·Caddy·HTTPS 통합 검증(임시 앱을 배포 후 정리):

```sh
MINDUL_RUN_INTEGRATION=1 python3 tests/integration_deploy.py
```

Dockerfile 배포, Compose 전환, 저장된 환경변수, 달러 문자가 포함된 값,
HTTPS, 데이터 보존, 수동 롤백, 실패 시 자동 복구와 테스트 리소스 정리를 검증합니다.
