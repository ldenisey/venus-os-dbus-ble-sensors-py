#!/usr/bin/env python3
# Copyright 2026 TechBlueprints
# Licensed under the Apache License, Version 2.0 (the "License").
# See LICENSE file for details.
#
# Read upstream gattserver.py from argv[1], write a patched copy to argv[2]
# that neutralizes the 60s mass-disconnect behavior described in
# https://github.com/victronenergy/venus/issues/1587
#
# Exit codes:
#   0  patched file written
#   1  no patch needed (signature didn't match — assume upstream fixed it)
#   2  I/O or unexpected error
#
# The patched file is intended to be bind-mounted on top of the original;
# the original on rootfs is never modified.

import re
import sys


def patch(src_text):
    method_match = re.search(
        r"def _keep_alive_timer_timeout.*?(?=\n\tdef |\Z)",
        src_text,
        re.DOTALL,
    )
    if not method_match or "Disconnect()" not in method_match.group():
        return None
    if "timeout_add(60000" not in src_text:
        return None

    out = re.sub(
        r"(\tdef _keep_alive_timer_timeout\(self\):\n).*?(\n\t\treturn False)",
        r'\1\t\tlogger.info("Keep alive timeout (disconnects disabled)")'
        r"\n\t\tself._keepAliveTimer = None"
        r"\n\t\t# vesmart-safety: disconnect-all disabled (venus#1587)\2",
        src_text,
        count=1,
        flags=re.DOTALL,
    )
    out = re.sub(
        r"(\t\tself\._keepAliveTimer = GObject\.timeout_add\(60000,\s*self\._keep_alive_timer_timeout\))",
        "\t\tpass  # vesmart-safety: 60s timer disabled (venus#1587)",
        out,
        count=1,
    )

    if out == src_text:
        return None
    return out


def main(argv):
    if len(argv) != 3:
        print("usage: patcher.py <src> <dst>", file=sys.stderr)
        return 2
    src, dst = argv[1], argv[2]
    try:
        with open(src) as f:
            text = f.read()
    except OSError as e:
        print(f"patcher: read {src}: {e}", file=sys.stderr)
        return 2
    patched = patch(text)
    if patched is None:
        return 1
    try:
        with open(dst, "w") as f:
            f.write(patched)
    except OSError as e:
        print(f"patcher: write {dst}: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
