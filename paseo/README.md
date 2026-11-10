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

GitHub App creation/installation, ready semantics and daemon enrollment remain
unconfigured. The supported provider list does not imply a worker is online.

## Dispatch contract to implement after those inputs are known

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

The routing policy above is a contract, not an installed workflow. Activation and a
real GitHub → ready → eligible daemon → PR test remain pending external setup.
