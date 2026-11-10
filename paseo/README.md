# Eniveil issue-development Hub

This host runs Paseo Hub and PostgreSQL only. Development runs on separately enrolled
Paseo daemons; no agent CLI, subscription or development daemon is installed by this stack.
The only intended automation is work on eligible ready issues in `MindulMendul/eniveil`.
No trigger is currently enabled and no existing issues are backfilled.

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

1. Create a dedicated GitHub App through Hub's Apps → GitHub guide and install it
   on **only `MindulMendul/eniveil`**. This assistant's GitHub connection does not grant
   a running third-party Hub its own persistent credentials. Use the generated Hub
   callback, setup and webhook URLs. Save private key/client secret in Hub, not Git.
2. On each work machine, install Paseo and its provider CLI, authenticate that CLI,
   clone the repository, and enroll using `paseo hub login https://paseo.eniveil.com`
   and `paseo hub connect`. Grant `paseo hub permissions grant hub.execute` there.
3. Record daemon slug, repository directory, provider/model/mode and supported task
   types. An offline or unconfigured provider must not receive development work.
4. Confirm whether Ready means a GitHub Projects Status field or a repository label.
   Hub natively supports `github.issue_label_added`; it does not currently expose a
   native `projects_v2_item` trigger. A Projects workflow needs a verified adapter,
   and personal Projects may require polling with separately authorized project access.

Ready means the GitHub Projects **Status** field. Codex and Claude will run on
a separate PC. GitHub App creation/installation, the exact board URL, project-read
credentials and daemon enrollment remain unconfigured. The supported provider list does not imply a worker is online.

## Projects Status router (implemented; disabled pending connections)

- Accept only open issues in `MindulMendul/eniveil` that actually transitioned into
  the agreed Ready state. Re-fetch the issue/status before claiming work. Do not
  infer approval from an unchecked checklist, issue title or stale webhook.
- Persist pending work and delivery deduplication; reconnects/re-deliveries must not
  start a second development session for the same issue readiness generation.
- Select an enrolled `hub.execute` daemon with the configured provider, a recent
  connection and available capacity; choose the least-loaded eligible candidate,
  then configured priority for ties. Reserve capacity atomically. Keep work queued
  if no candidate is eligible, rather than routing to an unknown model or machine.
- Claim one issue per worker initially. Enforce deadlines and retain explicit failure
  states; do not silently retry work whose remote launch outcome is unknown.
- Agent prompt: work in an isolated worktree from latest dev; read AGENTS.md; follow
  the repository issue/Goal process; implement only the issue; verify using the repo's
  current commands; open a feature/fix → dev PR. Do not promote qa/main or deploy.
  Required reviews/checks/human approval must follow the repository's current policy.
- Record the selected daemon/agent, issue, run ID, failure and resulting PR. Do not
  log GitHub tokens, session credentials or issue body content in operational logs.

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

Configure the ignored `paseo/.router.json` after the PC enrolls. Set the board number,
Hub GitHub connection slug and each worker's daemon slug, cwd, provider, native mode,
optional model and unique trigger name. For example, two workers can refer to the same
PC with providers `codex` and `claude`; only one development task runs on that PC at once.
Provider/mode/path availability is validated by Hub during bundle installation.
Only Hub-known agent executions are counted; interactive PC work not reported to Hub
and provider subscription/quota availability cannot be inferred by this router.

Runtime credentials belong in ignored `paseo/.router.env` (0600):
`PROJECTS_GITHUB_TOKEN` with Projects read access and `PASEO_ROUTER_API_KEY` scoped to
`runs:dispatch`. This is distinct from the development steps' GitHub App credentials.
A GitHub App installation must have appropriate Projects permission; personal Projects
can require user-authorized project access instead. Never paste secrets into chat.

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
`~/.local/share/mindul-paseo-router/queue.sqlite` holds observed transitions, capacity
reservations, run IDs and outcomes; backups use SQLite's online backup API. Repeated
polls, webhook re-deliveries and restarts do not requeue the same transition. A crashed
or timed-out HTTP handoff becomes `uncertain` and holds its capacity until the Hub's
matching durable delivery/run is found or an operator investigates it. It is never
silently sent to the other provider. Project changes require a separate queue database.
The queue does not change Projects status or automatically retry completed failures.

Activation and a real Projects Ready → eligible PC provider → development PR test
remain pending project credentials, GitHub App authority and PC enrollment. Current
state is installed **but disabled**; synthetic tests are not a real-agent integration test.


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
