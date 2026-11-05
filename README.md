# mindul-ai-infra

Docker Compose로 Ollama, Kafka, 개발 도구, Portainer와 Caddy를 실행합니다.

## 실행

`.env.example`을 `.env`로 복사하고 비밀번호를 설정한 다음 실행합니다.

```bash
docker compose up -d
docker compose logs -f ollama-init
```

`ollama-init`은 Ollama가 준비되면 기본 모델 `qwen3.5:4b`를 다운로드하고 종료합니다.
모델은 `ollama_data` 볼륨에 저장되며, 최초 다운로드가 완료되어야 채팅할 수 있습니다.
다른 모델은 `.env`의 `OLLAMA_MODEL`로 지정하고 요청의 `model`에도 같은 이름을 사용하세요.
다운로드 후에는 요청 시 모델이 메모리에 로드됩니다.

## 기존 서버의 모델 업그레이드

기본 모델을 Qwen 3 4B Instruct에서 Qwen 3.5 4B (`qwen3.5:4b`)로 변경했습니다.
기존 `.env`에 `OLLAMA_MODEL`이 있으면 `qwen3.5:4b`로 바꾸세요.
실행 이미지는 검증한 digest로 고정합니다. 이미지 변경은 `ops/README.md`의 절차로 적용하고 모델을 다운로드합니다.

```bash
docker compose pull ollama ollama-init
docker compose up -d ollama
docker compose run --rm ollama-init
docker compose exec ollama ollama list
```

다운로드가 완료되면 아래 채팅 API 예제로 응답을 확인하세요.
호출하는 앱에서도 요청의 `model`을 `qwen3.5:4b`로 변경해야 합니다.
기존 모델은 자동 삭제하지 않습니다.

모델 정보: https://ollama.com/library/qwen3.5:4b

## 채팅 API

```bash
curl --fail-with-body https://mindul-server.duckdns.org/api/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.5:4b",
    "messages": [{"role": "user", "content": "안녕! 한국어로 짧게 자기소개해 줘."}],
    "stream": false,
    "think": false
  }'
```

답변은 JSON의 `message.content`에 있습니다. `stream: true`이면 NDJSON으로 순차 응답합니다.
대화를 이어갈 때는 이전 사용자 메시지와 assistant 답변을 `messages`에 함께 보내세요.
첫 요청은 모델 로딩 때문에 더 오래 걸릴 수 있습니다. GPU 없는 서버에서는 생성 속도가 느릴 수 있습니다.

서버 내부에서는 `http://localhost:11434/api/chat`, 같은 Docker 네트워크에서는
`http://ollama:11434/api/chat`을 사용할 수 있습니다.

Caddy는 HTTPS의 `/api/chat`만 Ollama로 전달하며 나머지 경로는 기존 Portainer로 전달합니다.
현재 채팅 API에는 인증이 없습니다. 기존 Compose의 직접 공개 포트 `11434`는 Ollama의 전체 API를 제공합니다.

## 확인

```bash
docker compose ps -a ollama ollama-init
docker compose exec ollama ollama list
docker compose exec ollama ollama ps
```

API 규격: https://docs.ollama.com/api/chat

## Grafana 모니터링

https://mindul-server.duckdns.org/grafana/ 에서 서버·컨테이너·웹 RUM/API·AI 대시보드를 확인합니다.
모니터링은 별도 `monitoring/compose.yaml`로 실행하며 설정·대시보드는 저장소 파일로 관리합니다.
초기 계정은 `admin`, 비밀번호는 git에서 제외된 `monitoring/.env`에 있습니다.
구성, 데이터 보관 기간, 검증 명령과 수집 범위는 [monitoring/README.md](monitoring/README.md)를 참고하세요.

## 저장소 주소로 웹 앱 배포

추가 관리 서버 없이 `mindul` 명령어로 Git → 빌드 → 컨테이너 → HTTPS 배포를 실행합니다.
현재 서버에는 명령어가 설치되어 있습니다. 새 서버에서는 `scripts/install-mindul.sh`를 실행하세요.

```sh
mindul deploy https://github.com/owner/my-web.git
mindul deploy https://github.com/owner/my-web.git -e qa
mindul redeploy my-web
mindul logs my-web --follow
mindul rollback my-web
```

기본 주소는 `my-web.mindul-server.duckdns.org`이며 QA는 `my-web-qa.mindul-server.duckdns.org`입니다.
저장소에 Dockerfile 또는 Compose가 있어야 하며 필수 환경변수는 최초에 지정합니다.
설정 예제, 지원 범위, 데이터 보존과 복구는 [deployments/README.md](deployments/README.md)를 참고하세요.

## 운영 자동화와 복구

인프라의 실행 이미지 digest를 고정하고, CI는 검사한 커밋만 fast-forward 배포합니다.
기존 비밀 `.env`와 Grafana 비밀번호는 유지합니다. Kafka·코드 서버 설정도 영구 볼륨으로 보관합니다.

- 백업: 매일 03:20 KST, 암호화된 최근 3개 로컬 사본
- 복원 검증: 일요일 04:00 KST, 새 Docker 볼륨에 SQLite 복원·행 수 검증
- 정리: 일요일 04:30 KST, 사용하지 않는 빌드 캐시와 오래된 dangling 이미지
- HTTPS·인증서·컨테이너 점검: 5분마다
- Grafana 내부 경보 13개: 디스크·CPU·메모리·서비스·TLS·백업·복원 상태

[운영 대시보드](https://mindul-server.duckdns.org/grafana/d/mindul-ops)와
[운영·복원 설명서](ops/README.md)를 참고하세요. 현재 외부 백업 저장소와 외부 알림 채널은 연결하지 않았습니다.

```sh
mindul backup
mindul restore-check
mindul maintenance
mindul-ops status
```
