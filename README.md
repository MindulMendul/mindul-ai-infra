# mindul-ai-infra

Docker Compose로 Ollama, Kafka, 개발 도구, Portainer와 Caddy를 실행합니다.

## 실행

`.env.example`을 `.env`로 복사하고 비밀번호를 설정한 다음 실행합니다.

```bash
docker compose up -d
docker compose logs -f ollama-init
```

`ollama-init`은 Ollama가 준비되면 기본 모델 `qwen3:4b-instruct`를 다운로드하고 종료합니다.
모델은 `ollama_data` 볼륨에 저장되며, 최초 다운로드가 완료되어야 채팅할 수 있습니다.
다른 모델은 `.env`의 `OLLAMA_MODEL`로 지정하고 요청의 `model`에도 같은 이름을 사용하세요.
다운로드 후에는 요청 시 모델이 메모리에 로드됩니다.

## 채팅 API

```bash
curl --fail-with-body https://mindul-server.duckdns.org/api/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3:4b-instruct",
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
