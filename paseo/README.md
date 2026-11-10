# Eniveil issue-development Hub

Paseo Hub runs here, with the existing Ollama host providing LFM3 triage and Qwen XS/S
development. M/L development runs in a Codex Cloud environment connected to
`MindulMendul/eniveil`. Paseo polls the GitHub Project; its first scan is a baseline,
and the automation remains disabled until its credentials and workers are configured.

## Start and access

```bash
python3 scripts/install-paseo.py
docker compose --env-file paseo/.env -f paseo/compose.yaml ps
```

Public origin: `https://paseo.eniveil.com`. Create its DNS A record pointing to
`129.154.220.233`; Caddy handles HTTPS issuance and renewal after DNS resolves.
Until DNS is configured, the Hub is available on the host at `127.0.0.1:3180`.
The database has no published port. Hub's own port is bound only to loopback.

Initial operator: `admin@eniveil.com`; the generated password is in ignored
`paseo/.operator-credentials`, mode 0600. No email is sent. The bootstrap password
is removed from the runtime environment after the first successful start. Sign in
and replace the initial password before sharing access. Registration is invite-only
and public organization creation is disabled.

Images are pinned to immutable multiarchitecture digests including ARM64. Hub is
limited to 768 MiB/one CPU and PostgreSQL to 256 MiB/half a CPU. Normal installation
used approximately 208 MiB and 30 MiB respectively; workload may increase these.

## Required connections

1. Authenticate the Codex CLI on this host and create/publish a Codex Cloud environment
   that checks out **only `MindulMendul/eniveil`**. Put its exact environment ID in the
   enabled `codex-cloud` worker. Paseo invokes `codex cloud exec --env <id> --branch dev`;
   it does not need this chat's Codex workspace attached to eniveil.
2. Install Aider in a dedicated venv and configure its executable and an existing
   eniveil clone as `repo_path`:
   `python3 -m venv /home/ubuntu/.local/share/paseo-aider`, then
   `/home/ubuntu/.local/share/paseo-aider/bin/pip install aider-chat`. Pull `lfm3`
   and `qwen3.5:4b` into Ollama. The Qwen token needs eniveil issues read, contents
   write, and pull-request write access.
3. Configure a GitHub token with Projects read/write and eniveil issue read/write
   access. It reads Project fields and issue content, updates triaged issues, and
   posts rejection comments. Keep it in ignored `paseo/.router.env`.
4. Set the Project number and verify its Status options `Initial`, `Approve`,
   `Reject`, `Ready` and Size options `XS`, `S`, `M`, `L`. Priority is human
   managed and is never changed by the router.

## Initial triage and Ready execution (implemented; disabled pending setup)

- On each poll, process one newly observed open issue that entered Project Status
  `Initial`; the first complete snapshot is a baseline and does not bulk-triage old items.
- LFM3 alone classifies the issue as approve/reject, estimates Size, chooses matching
  existing labels and rewrites approved title/body into a clear implementation prompt.
  Priority remains untouched. Qwen is not used for triage.
- Approve assigns `MindulMendul`, applies the LFM3 title/body, labels and Size, then
  moves Status to `Approve`. Reject preserves the issue text, posts the LFM3 rationale
  as a deduplicated comment, and moves Status to `Reject`. Legacy `ignore` output is
  normalized to Reject.
- A human sets Priority and moves an approved issue to `Ready` to start development.
  The durable queue routes XS/S to the local Qwen Aider worker and M/L to the Codex
  Cloud worker. Codex Cloud must use its environment ID for the eniveil checkout.
- Both development workers use latest `dev`, read `AGENTS.md`, work in isolation,
  verify with `bash scripts/codex/verify.sh`, and open a typed PR to `dev`. Neither
  worker merges, promotes `qa/main`, or deploys.
- Initial and Ready transitions are deduplicated in SQLite. A failed LFM3 call or
  GitHub update remains pending; a timed-out remote development handoff is uncertain
  and is not silently retried. Do not log tokens, issue bodies, or model outputs.

## Operations

Grafana folder **Paseo**, dashboard UID `mindul-paseo`, reports container status,
CPU and memory. Execution/queue metrics will be added with the dispatcher; no fake
execution counts are provisioned.

The infrastructure deployer reconciles this stack on every deployment. The ordinary
encrypted backup includes both volumes and the ignored runtime/operator credentials.
For a consistent PostgreSQL snapshot it briefly stops Hub, then PostgreSQL, snapshots
PG, and starts PostgreSQL then Hub. Do not copy a running PostgreSQL data directory.
The Hub durable execution design must be respected by any dispatcher restart logic.
Before upgrades keep an encrypted backup: upstream DB migrations are forward-only.

`paseo/router` implements a complete-snapshot GraphQL poller, a WAL SQLite queue,
atomic claims and official Hub manual-run dispatch. The first successful scan records
a baseline without bulk-starting existing Ready items; later entries into Ready enqueue
work. The timer runs every minute with up to 30 seconds of jitter. Actual project
fields/options are checked, all pages must succeed, and issue status is rechecked
before dispatch. No issue labels substitute for Projects status.

Configure ignored `paseo/.router.json` from `paseo/router/config.example.json`.
Set the exact Project number, enable the Qwen XS/S worker with its eniveil clone path,
and enable the M/L Codex worker with the published eniveil Cloud environment ID.
The Project Size field must use the XS/S/M/L options; Ready issues without a recognized
Size stay queued.

Runtime credentials belong in ignored `paseo/.router.env` (0600):
`PROJECTS_GITHUB_TOKEN` needs Projects read/write and eniveil issue read/write;
`PASEO_QWEN_GITHUB_TOKEN` needs eniveil issue read, contents write, and pull-request
write; `OLLAMA_URL` points to the local Ollama API. Keep the existing
`PASEO_ROUTER_API_KEY` only for daemon-backed Hub dispatch. Never paste secrets into chat.

First render the bounded workflow bundle:

```bash
python3 paseo/router/runtime.py --render
```

For installation, provide a separate `PASEO_ROUTER_SETUP_KEY` scoped to
`configuration:validate` and `configuration:install`, then run:

```bash
python3 paseo/router/runtime.py --install-bundle
```

Hub checks the actual connected daemon/provider and installs immutable configuration.
The helper pins the returned version ID into the runtime config. Keep `enabled=false`
until a controlled ready-status smoke test is agreed; installation does not flip it.
The runtime key cannot replace configurations and a changed Hub revision is rejected.
The emitted JSON is valid YAML, submitted through the documented configuration API.

`mindul-paseo-router.timer` runs the router using the private environment file.
`~/.local/share/mindul-paseo-router/queue.sqlite` holds Ready jobs and
`triage.sqlite` holds Initial transitions and saved LFM3 decisions; both are backed up
using SQLite's online backup API. Repeated
polls, webhook re-deliveries and restarts do not requeue the same transition. A crashed
or timed-out HTTP handoff becomes `uncertain` and holds its capacity until the Hub's
matching durable delivery/run is found or an operator investigates it. It is never
silently sent to the other provider. Project changes require a separate queue database.
The queue does not change Projects status or automatically retry completed failures.

Activation and a real Initial → LFM3 triage → human Ready → size-routed PR run remain
pending credentials, an eniveil Codex Cloud environment, an Aider installation and a
configured Qwen repository clone. The example config remains disabled until these are ready.


## Separate-PC enrollment

Install Paseo and authenticate the Codex/Claude CLIs on the work PC. Run the following
from that PC's repository directory, not this Ubuntu Hub server:

```bash
paseo hub login https://paseo.eniveil.com
paseo hub permissions grant hub.execute
paseo hub status
```

During interactive login, accept connecting this daemon. If that prompt was skipped,
run `paseo hub connect` once. Do not repeat connect on an already-enrolled daemon.
The PC needs outbound HTTPS/WebSocket connectivity to the Hub, without exposing a
new inbound port. Use the actual local clone directory when configuring each worker.
PC sleep/offline leaves pending jobs queued. After enrollment provide the Projects
board URL and local clone path; provider model/mode choices must come from the connected
daemon catalog, not invented model names.
