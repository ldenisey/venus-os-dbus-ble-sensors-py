#!/bin/sh
# victron-bluetooth-safety.sh — Stop vesmart-server from mass-disconnecting BLE
# Version: 2.0.0
#
# Copyright 2026 TechBlueprints
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Venus OS upstream issue:
#   https://github.com/victronenergy/venus/issues/1587
#
# This installer follows the Venus OS wiki pattern for persistent
# customizations (data partition + /data/rc.local boot hook) and applies
# its fix using bind mounts, so the rootfs is never modified.
#
#   patch    bind-mount a regex-patched gattserver.py over the upstream
#            file. vesmart-server keeps running; VictronConnect over BLE
#            keeps working; only the 60s mass-disconnect timer is neutered.
#
#   disable  bind-mount a no-op run script over vesmart-server's service
#            run script. vesmart-server never starts. Lose VictronConnect
#            over BLE; gain a fully version-agnostic fix.
#
# Usage (on the Cerbo, after copying this directory to /data):
#   sh victron-bluetooth-safety.sh install [--mode patch|disable]
#   sh victron-bluetooth-safety.sh uninstall
#   sh victron-bluetooth-safety.sh status
#   sh victron-bluetooth-safety.sh apply        # invoked from rc.local

VERSION="2.0.0"
INSTALL_DIR="/data/victron-bluetooth-safety"
RUN_DIR="/run/victron-bluetooth-safety"
RC_LOCAL="/data/rc.local"
RC_MARKER_BEGIN="# victron-bluetooth-safety BEGIN"
RC_MARKER_END="# victron-bluetooth-safety END"

GATTSERVER_PATH="/opt/victronenergy/vesmart-server/gattserver.py"
SERVICE_RUN_LIVE="/service/vesmart-server/run"
SERVICE_RUN_CANONICAL="/opt/victronenergy/service/vesmart-server/run"

_log() { echo "[bt-safety] $*"; }

_is_venus_os() { [ -f /opt/victronenergy/version ]; }

_is_bind_mounted() {
    # Match the path as a mount target in /proc/mounts. The path appears
    # as the second field; use awk to avoid pathname-as-substring issues.
    awk -v p="$1" '$2 == p {found=1} END {exit !found}' /proc/mounts
}

_resolve_run_target() {
    # Return the path that daemontools is actually reading. Prefer the
    # live tmpfs path if it resolves to a real file; fall back to the
    # canonical /opt path so disable mode still works pre-overlay.
    if [ -e "$SERVICE_RUN_LIVE" ]; then
        readlink -f "$SERVICE_RUN_LIVE" 2>/dev/null || echo "$SERVICE_RUN_LIVE"
    else
        echo "$SERVICE_RUN_CANONICAL"
    fi
}

_restart_vesmart() {
    [ -d /service/vesmart-server ] && svc -t /service/vesmart-server 2>/dev/null
}

# ---------------------------------------------------------------- apply patch

_apply_patch_mode() {
    [ -f "$GATTSERVER_PATH" ] || { _log "vesmart-server not installed, nothing to do"; return 0; }

    if _is_bind_mounted "$GATTSERVER_PATH"; then
        _log "patch already active (bind mount present on $GATTSERVER_PATH)"
        return 0
    fi

    mkdir -p "$RUN_DIR"
    patched="$RUN_DIR/gattserver.py"

    python3 "$INSTALL_DIR/patcher.py" "$GATTSERVER_PATH" "$patched"
    rc=$?
    case "$rc" in
        0) ;;  # patched
        1) _log "no patch needed (regex did not match — upstream may have fixed venus#1587)"; return 0 ;;
        *) _log "ERROR: patcher failed (exit $rc)"; return 1 ;;
    esac

    chmod "$(stat -c %a "$GATTSERVER_PATH")" "$patched"

    if mount --bind "$patched" "$GATTSERVER_PATH"; then
        _log "patch applied: bind-mounted $patched -> $GATTSERVER_PATH"
        _restart_vesmart
    else
        _log "ERROR: bind mount failed on $GATTSERVER_PATH"
        return 1
    fi
}

# -------------------------------------------------------------- apply disable

_apply_disable_mode() {
    target=$(_resolve_run_target)
    [ -e "$target" ] || { _log "$target not present, nothing to do"; return 0; }

    if _is_bind_mounted "$target"; then
        _log "disable already active (bind mount present on $target)"
        return 0
    fi

    chmod +x "$INSTALL_DIR/noop-run"

    if mount --bind "$INSTALL_DIR/noop-run" "$target"; then
        _log "disabled vesmart-server: bind-mounted noop-run -> $target"
        _restart_vesmart
    else
        _log "ERROR: bind mount failed on $target"
        return 1
    fi
}

# -------------------------------------------------------------------- unmount

_unmount_all() {
    # Try every path we might have bind-mounted onto. Ignore failures —
    # umount of an unmounted path is the no-op we want.
    any=0
    for p in "$GATTSERVER_PATH" "$SERVICE_RUN_LIVE" "$SERVICE_RUN_CANONICAL"; do
        if _is_bind_mounted "$p"; then
            if umount "$p" 2>/dev/null; then
                _log "unmounted $p"
                any=1
            else
                _log "WARNING: umount $p failed"
            fi
        fi
    done
    [ "$any" = 1 ] && _restart_vesmart
    return 0
}

# ------------------------------------------------------------------- rc.local

_install_rc_hook() {
    if [ -f "$RC_LOCAL" ] && grep -q "$RC_MARKER_BEGIN" "$RC_LOCAL"; then
        _log "rc.local hook already present"
        return 0
    fi

    if [ ! -f "$RC_LOCAL" ]; then
        printf '#!/bin/sh\n' > "$RC_LOCAL"
        chmod +x "$RC_LOCAL"
    fi

    cat >> "$RC_LOCAL" <<RCEOF
$RC_MARKER_BEGIN
[ -x $INSTALL_DIR/victron-bluetooth-safety.sh ] && \\
    $INSTALL_DIR/victron-bluetooth-safety.sh apply
$RC_MARKER_END
RCEOF
    _log "rc.local hook installed"
}

_uninstall_rc_hook() {
    [ -f "$RC_LOCAL" ] && grep -q "$RC_MARKER_BEGIN" "$RC_LOCAL" || {
        _log "no rc.local hook to remove"
        return 0
    }
    sed -i "/$RC_MARKER_BEGIN/,/$RC_MARKER_END/d" "$RC_LOCAL"
    _log "rc.local hook removed"
}

# ----------------------------------------------------------------- subcommands

do_install() {
    mode="${1:-patch}"
    case "$mode" in patch|disable) ;; *) _log "ERROR: unknown mode '$mode'"; return 1 ;; esac

    _is_venus_os || { _log "ERROR: not Venus OS"; return 1; }

    [ -f "$INSTALL_DIR/patcher.py" ] || {
        _log "ERROR: $INSTALL_DIR/patcher.py missing — copy this directory to $INSTALL_DIR first"
        return 1
    }

    chmod +x "$INSTALL_DIR/victron-bluetooth-safety.sh" "$INSTALL_DIR/noop-run" 2>/dev/null

    # If switching modes, drop the previous bind mounts first.
    _unmount_all

    printf '%s\n' "$mode" > "$INSTALL_DIR/mode"
    _log "mode=$mode written to $INSTALL_DIR/mode"

    _install_rc_hook
    do_apply
}

do_uninstall() {
    _is_venus_os || { _log "ERROR: not Venus OS"; return 1; }
    _uninstall_rc_hook
    _unmount_all
    rm -f "$INSTALL_DIR/mode"
    _log "uninstalled. Files remain in $INSTALL_DIR — remove manually if desired."
}

do_apply() {
    mode=$(cat "$INSTALL_DIR/mode" 2>/dev/null)
    case "$mode" in
        patch)   _apply_patch_mode ;;
        disable) _apply_disable_mode ;;
        "")      _log "no mode configured ($INSTALL_DIR/mode missing) — skipping"; return 0 ;;
        *)       _log "ERROR: unknown mode '$mode' in $INSTALL_DIR/mode"; return 1 ;;
    esac
}

do_status() {
    _log "version $VERSION"
    if [ -f "$INSTALL_DIR/mode" ]; then
        _log "configured mode: $(cat "$INSTALL_DIR/mode")"
    else
        _log "configured mode: (none — not installed)"
    fi

    if _is_bind_mounted "$GATTSERVER_PATH"; then
        _log "ACTIVE (patch): bind mount on $GATTSERVER_PATH"
    fi
    for p in "$SERVICE_RUN_LIVE" "$SERVICE_RUN_CANONICAL"; do
        if _is_bind_mounted "$p"; then
            _log "ACTIVE (disable): bind mount on $p"
        fi
    done

    if [ -f "$RC_LOCAL" ] && grep -q "$RC_MARKER_BEGIN" "$RC_LOCAL"; then
        _log "rc.local hook: installed"
    else
        _log "rc.local hook: not installed"
    fi
}

# ----------------------------------------------------------------------- main

_usage() {
    cat <<EOF
victron-bluetooth-safety $VERSION

Usage:
  $0 install [--mode patch|disable]   default: patch
  $0 uninstall
  $0 status
  $0 apply                            (invoked from /data/rc.local at boot)

Modes:
  patch    bind-mount a regex-patched gattserver.py over the upstream file.
           Keeps VictronConnect BLE working; neutralizes only the 60s
           mass-disconnect timer.
  disable  bind-mount a no-op run script over vesmart-server's service
           run script. Disables vesmart-server entirely (loses
           VictronConnect BLE; simplest, fully version-agnostic).
EOF
}

_parse_install_args() {
    mode=patch
    shift  # drop "install"
    while [ $# -gt 0 ]; do
        case "$1" in
            --mode) mode="$2"; shift 2 ;;
            --mode=*) mode="${1#--mode=}"; shift ;;
            *) _log "ERROR: unexpected arg '$1'"; return 1 ;;
        esac
    done
    do_install "$mode"
}

_main() {
    case "${1:-}" in
        install)             _parse_install_args "$@" ;;
        uninstall|remove)    do_uninstall ;;
        status)              do_status ;;
        apply)               do_apply ;;
        --version|-V)        echo "victron-bluetooth-safety $VERSION" ;;
        --help|-h|"")        _usage ;;
        *)                   _log "ERROR: unknown command '$1'"; _usage; return 1 ;;
    esac
}

_main "$@"
