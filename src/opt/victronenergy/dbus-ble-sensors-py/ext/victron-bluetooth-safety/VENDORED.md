# Vendored: victron-bluetooth-safety

## Source

- Repository: https://github.com/TechBlueprints/victron-bluetooth-safety
- Commit: `9a2485534c060207654d53857cc26122359d36bf`
- License: Apache 2.0 (see `LICENSE`)

## What this is

A standalone fix for the `vesmart-server` mass-disconnect bug on Venus OS
documented in
[victronenergy/venus#1587](https://github.com/victronenergy/venus/issues/1587).

When *any* BLE device connects to the Cerbo, `vesmart-server` starts a
hardcoded 60-second timer.  When that timer fires, it iterates **every
connected BLE device on every adapter** and disconnects them — batteries,
tank sensors, relay switches, everything.  This makes stable BLE
connections impossible for any third-party service while
`vesmart-server` is running.

`dbus-ble-sensors-py` itself maintains long-lived BLE *advertisement*
subscriptions through `bluetoothd` and is **directly affected** by the
mass-disconnect: every 60s, all BLE adapters get a flood of disconnect
events that disrupt scanning and re-trigger BlueZ state machines.
Vendoring this fix is a hard requirement for the fork to work reliably
alongside other BLE services on the Cerbo.

## How it gets applied

Upstream now applies its fix entirely with `mount --bind`, following the
[Venus OS wiki guidance](https://github.com/victronenergy/venus/wiki/howto-add-a-driver-to-Venus#how-to-make-changes-that-dont-get-lost-on-a-firmware-update)
for persistent customizations: all files live under `/data/`, a single
`/data/rc.local` boot hook re-establishes the fix on every reboot, and
the rootfs is never modified.

`install.sh` (Step 5.5) copies this directory to
`/data/victron-bluetooth-safety/` and runs:

    sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh install --mode patch

That command:

- Writes `/data/victron-bluetooth-safety/mode` (selects `patch` mode).
- Adds a hook block to `/data/rc.local` that calls `... apply` at boot.
- Runs `apply` immediately: regenerates a patched copy of `gattserver.py`
  (via `patcher.py`'s version-agnostic regex), bind-mounts it on top of
  `/opt/victronenergy/vesmart-server/gattserver.py`, restarts
  `vesmart-server`.

The patched copy is re-derived from the live upstream `gattserver.py`
on every boot, so a future Victron fix to venus#1587 cleanly falls
through to no patch (regex won't match).

### Modes

- `--mode patch` (default) — bind-mount a patched `gattserver.py`.
  `vesmart-server` keeps running, VictronConnect over BLE keeps
  working, only the 60s mass-disconnect timer is neutered.  This is
  what `install.sh` selects.

- `--mode disable` — bind-mount a no-op `run` script over
  `vesmart-server`'s service run script.  Disables `vesmart-server`
  entirely; loses VictronConnect over BLE; fully version-agnostic.
  Power users who don't need VictronConnect can switch to this with:

      sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh install --mode disable

### Status / uninstall

    sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh status
    sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh uninstall

Uninstall is a single `umount` plus removal of the `/data/rc.local`
block — there is no patched file to revert because the rootfs was
never modified.

## Why we vendor instead of fetching

- Air-gapped / poor-connectivity Cerbo installs (RVs, boats, remote
  monitoring sites) can't necessarily reach GitHub at install time.
- `install.sh` already fetches the dbus-ble-sensors-py source via git;
  we don't want to add a second remote dependency to a security-relevant
  patch.
- Pinning the source SHA in tree means we can verify exactly which
  version of the fix shipped with which release of this fork.

## Updating

To pick up a new upstream commit:

    cd /tmp
    git clone --depth=1 https://github.com/TechBlueprints/victron-bluetooth-safety.git
    cd victron-bluetooth-safety
    git rev-parse HEAD                    # note the new SHA

Then in this fork:

    DEST=src/opt/victronenergy/dbus-ble-sensors-py/ext/victron-bluetooth-safety
    cp /tmp/victron-bluetooth-safety/victron-bluetooth-safety.sh "$DEST/"
    cp /tmp/victron-bluetooth-safety/patcher.py                  "$DEST/"
    cp /tmp/victron-bluetooth-safety/noop-run                    "$DEST/"
    cp /tmp/victron-bluetooth-safety/LICENSE                     "$DEST/"
    cp /tmp/victron-bluetooth-safety/README.md                   "$DEST/"
    # Update the SHA in this file.

## Local modifications

None.  Files are byte-identical to upstream commit
`9a2485534c060207654d53857cc26122359d36bf`.
