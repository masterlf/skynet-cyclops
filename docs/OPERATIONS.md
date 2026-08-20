# Operations

## TL;DR

Run Cyclops as a systemd user timer in observe-only mode. Healthy ticks are silent. If state is missing, malformed or stale, Cyclops reports `unknown` or `critical`; it never manufactures health.

## Installation model

The repository helper is dry-run by default. With `--apply`, it installs only the user unit and example configuration templates and creates the private state directory:

```text
~/.config/systemd/user/skynet-cyclops.service
~/.config/systemd/user/skynet-cyclops.timer
~/.config/skynet-cyclops/config.yaml
~/.config/skynet-cyclops/mission.yaml
~/.local/state/skynet-cyclops/
```

Install the Python package separately to provide `~/.local/bin/skynet-cyclops`. The helper installs a functional synthetic mission path and never overwrites an existing operator config or mission on rerun. It never installs the package, enables or starts the timer, or enables the dashboard plugin. System packages or root installation are not required for the user-service path.

## Safe rollout

1. Validate the manifest.
2. Run bootstrap in dry-run mode.
3. Apply bootstrap only after reviewing the complete card graph.
4. Run `cyclops tick` manually with a disposable state directory.
5. Install the timer in observe-only mode.
6. Verify three healthy ticks and the status projection.
7. Enable the read-only dashboard integration.

## Commands

```bash
cyclops manifest validate /path/to/mission.yaml
cyclops bootstrap /path/to/mission.yaml --dry-run
cyclops bootstrap /path/to/mission.yaml --apply --config /path/to/config.yaml
cyclops tick --config /path/to/config.yaml          # silent on success
cyclops tick --config /path/to/config.yaml --json   # explicit JSON output
cyclops status --config /path/to/config.yaml --json
```

## Manager wake-up installation and activation contract

The v0.2 manager path remains disabled until an operator installs paused Hermes jobs through the
supported profile-local `cronjob` tool, runs the disposable compatibility checks, and explicitly
enables the router. Cyclops never writes Hermes cron stores or enables jobs. Review the strict
machine-readable initial-install spec with:

```bash
cyclops manager install --profile default --home-delivery telegram
```

Dry-run changes no files. Before staging, call `cronjob(action="list")` in the canonical `default`
profile and save its exact `jobs` array as a private snapshot. Initial install requires both stable
names to be absent. Then stage only private scripts/config and emit the nonce-bound tool spec:

```bash
cyclops manager install --profile default --home-delivery telegram --apply \
  --snapshot /private/path/cron-jobs.json --hermes-home /private/default-profile-home
```

The profile-local agent consumes only the emitted `cyclops-cron-install/v1` operations. After each
create, update, and pause it calls `cronjob(action="list", include_disabled=true)` and compares every
tool-visible security field with the snapshot-bound expected definition. Each create is immediately
paused and verified; any mismatch or later failure triggers reverse rollback followed by another
full-field readback. Upgrades use `--operation upgrade --previous-spec /private/path/manager-install.json`,
require exactly one paused full-field tool snapshot per stable name, and restore through
`cronjob.update` in reverse order. A conflict or ambiguous/missing snapshot fails before mutation.

Run the wheel-installed `cyclops-verify-hermes-cron-seams` command before activation (the repository
wrapper `scripts/verify_hermes_cron_seams.py` is equivalent). The verifier always creates and removes
its own synthetic temporary `default` profile; it does not accept or mutate a configured profile. Its
`cyclops-hermes-seam-evidence/v1` output behaviorally verifies that configured
`enabled_toolsets=["no_mcp"]` resolves to zero tools under non-dispatcher cron context and that an
empty no-agent courier run is silent with zero agent construction, and a synthetic create/pause/list/
remove cycle exposes exact full-field cronjob readback. This is partial seam evidence;
the profile-local installer must still verify stable paused identity, local delivery, and bounded
exactly-one output matching before activation. Never substitute a literal stored
`enabled_toolsets=[]` for the manager job.

The staged scripts call only:

```bash
cyclops manager router --config /path/to/config.yaml   # final wakeAgent JSON gate
cyclops manager courier --config /path/to/config.yaml # empty stdout when no intent exists
```

### Activation

Activation consumes an owner-private (`0600`), duplicate-key-free
`cyclops-manager-current-evidence/v1` envelope for the exact router/courier IDs and complete
`cyclops-hermes-seam-evidence/v1` report. Any version, collection timestamp, or definition copy in
that envelope is preflight context only and cannot authorize. On every dry run and apply collection,
Cyclops executes the configured binary with fixed read-only argv: `hermes --version` and
`hermes cron show EXACT_JOB_ID --json` for each ID. Only bounded, valid UTF-8, closed-schema
`hermes-cron-definition/v1` responses containing the full prompts are normalized and hashed. A
`cronjob list` prompt preview, desired install spec, private cron storage, replayed envelope, or
session history is never sufficient evidence.

```bash
# Both forms perform identical validation; the first writes nothing.
cyclops manager activate --config /path/to/config.yaml \
  --evidence /private/current-evidence.json \
  --hermes-home /private/.hermes/profiles/default
cyclops manager activate --config /path/to/config.yaml \
  --evidence /private/current-evidence.json \
  --hermes-home /private/.hermes/profiles/default --apply
```

Apply acquires a private lock, re-reads the local bindings/spec/scripts, recollects the current Hermes
version and both definitions, requires both exact jobs paused, atomically writes
`manager-activation.json`, and verifies readback. It never resumes a job. Router and tick read the
private binding envelope beside the ledger by default (or the explicit router `--evidence` path),
then independently recollect version plus both definitions immediately before the shared validator
on every invocation. Missing command, nonzero exit, timeout, oversized stdout/stderr, invalid UTF-8,
malformed JSON, wrong protocol/ID/state/schema, or definition drift projects `unsupported` with
`wake_enabled=false` and denies before lease or budget mutation. There is no replayable freshness
window.

Deactivation is also dry-run first and idempotent:

```bash
cyclops manager deactivate --config /path/to/config.yaml
cyclops manager deactivate --config /path/to/config.yaml --apply
```

Task-, run-, board-, workspace-, or delegated-child-scoped router execution is denied before a
lease or wake budget is acquired. The manager can return only `NOOP` or `ESCALATE`; it cannot
complete, unblock, retry, publish, deploy, or edit anything.

## Health

A healthy supervisor has:

- recent `heartbeat_at`;
- increasing `tick_seq`;
- no schema or manifest error;
- fresh Kanban collection;
- mode `observe` for the first public release.

## Stop and rollback

```bash
systemctl --user stop skynet-cyclops.timer
systemctl --user disable skynet-cyclops.timer
```

Stopping Cyclops does not stop Hermes or alter Kanban. Remove the generated projection and private ledger only after the timer is stopped. Kanban remains authoritative.

For a manager-package rollback, first apply deactivation and verify the shared validator projects
`wake_enabled=false`. Then pause router and courier using supported `cronjob` operations and obtain
exact full-definition readback proving both are paused. Only then restore package/spec/scripts.
Never resume v0.2.0 jobs: that release does not enforce activation attestation. If paused readback
cannot be proven, leave v0.2.1 installed and fail the rollback closed.

## Troubleshooting

- **Projection stale:** check the timer and service journal.
- **Manifest rejected:** fix the named schema/graph error; do not bypass validation.
- **Hermes CLI unavailable:** Cyclops exits with code 3 and leaves the prior projection intact; treat it as stale until a successful tick replaces it.
- **Ledger corrupt:** preserve the file for diagnosis; reinitialize in observe-only mode.
- **Manager compatibility unsupported:** leave both jobs paused. Do not bypass the zero-agent,
  non-task-scope, zero-tool, private-result, local-delivery, or paused-readback checks.
- **Manager dead letter:** use the stable decision packet ID to deduplicate delivery, inspect the
  private ledger locally, and keep Kanban changes manual.
- **Host unavailable:** use an external uptime monitor; local components cannot report complete host loss.

## Dashboard plugin

The plugin manifest declares a visible `/skynet-cyclops` tab positioned after Kanban, one read-only API route, and static JS/CSS entry files. Copy and enable it only through the supported Hermes Dashboard plugin workflow. Skynet-Cyclops does not auto-enable the plugin, and the manifest intentionally contains no invented default-enablement field.
