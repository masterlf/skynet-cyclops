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

## Troubleshooting

- **Projection stale:** check the timer and service journal.
- **Manifest rejected:** fix the named schema/graph error; do not bypass validation.
- **Hermes CLI unavailable:** Cyclops exits with code 3 and leaves the prior projection intact; treat it as stale until a successful tick replaces it.
- **Ledger corrupt:** preserve the file for diagnosis; reinitialize in observe-only mode.
- **Host unavailable:** use an external uptime monitor; local components cannot report complete host loss.

## Dashboard plugin

The plugin manifest declares a visible `/skynet-cyclops` tab positioned after Kanban, one read-only API route, and static JS/CSS entry files. Copy and enable it only through the supported Hermes Dashboard plugin workflow. Skynet-Cyclops does not auto-enable the plugin, and the manifest intentionally contains no invented default-enablement field.
