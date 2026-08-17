# Operations

## TL;DR

Run Cyclops as a systemd user timer in observe-only mode. Healthy ticks are silent. If state is missing, malformed or stale, Cyclops reports `unknown` or `critical`; it never manufactures health.

## Installation model

The packaged installer will place:

```text
~/.local/bin/skynet-cyclops
~/.config/systemd/user/skynet-cyclops.service
~/.config/systemd/user/skynet-cyclops.timer
~/.config/skynet-cyclops/config.yaml
~/.local/state/skynet-cyclops/
```

System packages or root installation are not required for the user-service path.

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
cyclops tick --config /path/to/config.yaml
cyclops status --config /path/to/config.yaml
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
- **Hermes CLI unavailable:** Cyclops reports collection failure and exits nonzero.
- **Ledger corrupt:** preserve the file for diagnosis; reinitialize in observe-only mode.
- **Host unavailable:** use an external uptime monitor; local components cannot report complete host loss.
