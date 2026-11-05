# Mindul monitoring

Grafana: https://mindul-server.duckdns.org/grafana/

Login: `admin`. Initial password is stored only in the ignored `monitoring/.env`
(mode 0600). Do not commit it or send it to frontend clients.

## Start / update

From `mindul-ai-infra`:

```sh
docker compose --env-file monitoring/.env -f monitoring/compose.yaml up -d --build
docker compose --env-file monitoring/.env -f monitoring/compose.yaml ps
docker compose --env-file monitoring/.env -f monitoring/compose.yaml logs --tail 50
docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

On a fresh server copy `.env.example` to `.env` and replace the password before
starting. Grafana reads the bootstrap password only when its data volume is first
initialized. Later password changes are made through Grafana itself; updating
the environment alone does not reset an existing account.

## Provisioned dashboards

- **Mindul · Infrastructure**: host CPU, RAM, disk, load; all containers' CPU,
  memory and network; scrape health. 100% container CPU represents one CPU core.
- **Mindul · Web RUM & API**: filter `production` / `qa`; page views and session
  starts, p75 LCP/INP/CLS/FCP/TTFB, browser exceptions, browser and server API
  latency, WebSocket failures/reconnects/timeouts, browser/OS/device/release
  breakdowns, sanitized error stacks and events.
- **Mindul · Ollama AI**: active requests, loaded model memory, request latency,
  errors, input/output tokens, generation speed and model loading time. The top
  cards show the last completed response speed (tokens/s), the weighted 5-minute
  generation average, speed including loading/prompt processing, and the last
  completion timestamp. Speeds update after a response completes, not per streamed
  chunk. The last-response cards retain the last measurement while idle.

Explore → Tempo searches service `murder-parker-web` or `murder-parker-api`.
Browser `traceparent` headers link fetch spans to the backend HTTP spans.
These are HTTP spans; database queries and internal game functions are not
individually instrumented. WebSocket connections have events and a live gauge,
not one indefinitely open trace.

## Data flow and boundaries

```text
Faro browser SDK → same-origin /telemetry/collect → telemetry-gateway
  → allowlist and privacy filter → Alloy → Loki (events/errors) + Tempo (traces)
  → Prometheus metrics → Grafana

Caddy /api/chat → telemetry-gateway → Ollama
Node Exporter / cAdvisor / web /metrics → Prometheus → Grafana
Backend HTTP spans → Tempo
```

Only Caddy exposes routes publicly. Prometheus, Loki, Tempo, exporter and gateway
ports have no host bindings. `/metrics` is blocked by Caddy on both web hosts.
Grafana requires login. Collection accepts the matching web Origin, caps batch
size at 256 KiB and rate-limits each environment; this is abuse control, not
proof that data came from a trusted user. Public clients can fabricate telemetry.

RUM has 100% sampling for this development deployment. There is no session replay,
DOM recording, console capture or form capture. The gateway removes user metadata,
invitation query strings, measurement attribution/DOM selectors, arbitrary custom
attributes, exception messages and trace event payloads. It retains pseudonymous
volatile session IDs, browser/OS versions and sanitized stack locations. No
nicknames, room codes, roles, votes or game content are deliberately collected.
Prompts, model answers, authorization headers and cookies are not logged.

The AI gateway meters `/api/chat` through the public HTTPS endpoint only. Direct
calls to Ollama's existing port 11434 bypass request/token metering, although
container resource metrics and loaded-model memory still cover Ollama. Existing
public Ollama access was not changed. AI tokens are known after a completed final
response; disconnected/incomplete streams may not have full token counts.
Unknown models are grouped under `other` to bound metric cardinality.

Prometheus: 7 days, at most 1 GB of TSDB blocks (WAL/head are additional).
Loki logs: 7 days. Tempo traces: 48 hours. Each container's Docker log rotates
at 10 MB × 2. Loki/Tempo time retention is not a strict disk quota. Watch the
disk panel and shorten retention or add storage if traffic grows.
Application/gateway counters reset on process restart; Prometheus handles resets
in rate/increase calculations. Requests before this installation are unavailable.
An empty INP panel means no completed user interaction has been reported yet.
LCP/CLS/INP can be finalized when the page becomes hidden. Percentile panels need
several scrapes; early sparse data can be empty or approximate.

Dashboard/config files are provisioned from Git; edit JSON/YAML here rather than
relying on UI-only changes. Persistent Docker volumes contain runtime history.
Do not use `down -v` when preserving that history.

## Verification

Gateway regression tests:

```sh
docker run --rm \
  -v "$PWD/monitoring/gateway/test_app.py:/app/test_app.py:ro" \
  mindul-monitoring-telemetry-gateway python -m unittest -v test_app
```

Web regression tests live in `murder-parker/apps/server`; browser verification
also checks real login, WebSocket, collector 202 responses, Web Vitals and the
Grafana dashboards. Test traffic is real validation traffic and will appear in
the initial monitoring history.

## Operations dashboard and managed alerting

`Mindul · Operations & Backups` adds encrypted backup age/result/size, isolated restore
verification age, maintenance, HTTPS, certificate expiry and required container state.
The **Mindul Operations** alert folder contains 13 provisioned rules. The default
receiver is the local gateway endpoint `/internal/alerts`; no external messages are
sent. External storage and notification destinations are not configured.

The Node Exporter textfile collector reads protected host-generated job metrics.
`ops/README.md` documents the systemd jobs, data scope, encryption key, retention,
image pinning and recovery procedure. Logs/traces/metrics history is excluded from
the default data backup; their definitions and dashboards are versioned in Git.

Grafana folders: Infrastructure (host and backups), AI, and Murder Mystery
(Web RUM; API, Errors & Traces). Existing `mindul-rum` links remain valid.
Murder Mystery dashboards retain their production/QA environment selector.

After successful deployment or removal, the CLI reconciles a `Web · <app>` folder.
Its Service dashboard covers container CPU, memory and network for that application's
environments. `python3 scripts/service_dashboards.py` reconciles manually; installation
also rebuilds these runtime files from the deployment registry. Application RUM, API
metrics, logs and traces require explicit instrumentation for each new application.
Folders organize navigation and do not provide tenant access isolation.
