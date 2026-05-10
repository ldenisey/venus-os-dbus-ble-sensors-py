# victron-bluetooth-safety

Stop Victron's `vesmart-server` on Venus OS from disconnecting **all** BLE
devices on **all** adapters every 60 seconds.

Upstream issue: [victronenergy/venus#1587](https://github.com/victronenergy/venus/issues/1587)

## The problem

When any BLE device connects to the Cerbo (even on a different adapter),
`vesmart-server` starts a hardcoded 60-second keep-alive timer. When that
timer fires (because the connected device isn't a VictronConnect client and
never sends a keep-alive), it disconnects **every** connected BLE device
it can find — batteries, sensors, everything.

This makes it impossible to maintain stable BLE connections for third-party
services (battery monitors, temperature sensors, relay switches, etc.)
while `vesmart-server` is running.

## How this fix works

This installer follows the [Venus OS wiki guidance for persistent
customizations](https://github.com/victronenergy/venus/wiki/howto-add-a-driver-to-Venus#how-to-make-changes-that-dont-get-lost-on-a-firmware-update):
all files live under `/data/` (which survives firmware updates) and a
single `/data/rc.local` hook re-establishes the fix at every boot.

**The rootfs is never modified.** The fix is applied entirely with
`mount --bind`, which shadows a file at the kernel mount-table level
without touching the underlying read-only filesystem. That means:

- No `mount -o remount,rw /` is required.
- Uninstall is a single `umount` — there is no patched file to revert.
- After a firmware update, `/data/rc.local` re-derives the patched copy
  from whatever new upstream `gattserver.py` shipped.

## Two modes

Pick whichever fits your setup:

### `--mode patch` (default)

Bind-mount a regex-patched copy of `gattserver.py` on top of the upstream
file. `vesmart-server` keeps running and **VictronConnect over BLE keeps
working**; only the 60s mass-disconnect timer is neutered.

The patch is regenerated on each boot from the live upstream file by
[`patcher.py`](patcher.py), so it is version-agnostic. If Victron fixes
the upstream bug, the regex won't match and we cleanly fall through to
no patch.

### `--mode disable`

Bind-mount a no-op `run` script over `vesmart-server`'s daemontools service
run script. `vesmart-server` never starts. **You lose VictronConnect over
BLE**, but the fix becomes byte-for-byte version-agnostic forever — there
is no Python source to keep regexing.

Choose this if you don't use VictronConnect on this Cerbo.

## Install

Copy the directory to `/data` and run the installer:

```bash
scp -r . root@cerbo:/data/victron-bluetooth-safety/
ssh root@cerbo 'sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh install'
```

Choose `--mode disable` instead if you want to fully disable
`vesmart-server`:

```bash
ssh root@cerbo 'sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh install --mode disable'
```

The installer will:

1. Verify Venus OS.
2. Write the chosen mode to `/data/victron-bluetooth-safety/mode`.
3. Add a hook to `/data/rc.local` that calls `... apply` at boot.
4. Run `apply` immediately so the fix takes effect without a reboot.

## Uninstall

```bash
ssh root@cerbo 'sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh uninstall'
```

This removes the `/data/rc.local` hook and unmounts any active bind
mounts. The files in `/data/victron-bluetooth-safety/` are left in place;
delete them manually if desired.

## Status

```bash
ssh root@cerbo 'sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh status'
```

Reports the configured mode, which bind mounts are currently active, and
whether the `/data/rc.local` hook is installed.

## Switching modes

Re-run `install` with the new `--mode`. The installer drops any active
bind mounts before re-applying.

## Files

| File | Purpose |
|------|---------|
| [`victron-bluetooth-safety.sh`](victron-bluetooth-safety.sh) | Installer / uninstaller / boot hook entry point |
| [`patcher.py`](patcher.py) | Regex patcher used by `--mode patch` |
| [`noop-run`](noop-run) | No-op service run script used by `--mode disable` |

## Compatibility

Tested on Venus OS v3.67 and v3.72 in `--mode patch`. `--mode disable`
has no version dependency.

## Development

A non-production Cerbo GX is available for testing at `root@dev-cerbo`.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
