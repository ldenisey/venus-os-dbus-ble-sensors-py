"""Sanity checks for the in-tree vendored victron_ble package.

Confirms that:

1. The vendored package is on the import path that the runtime uses.
2. The cryptography-backend patch in ``devices/base.py`` exposes the
   helpers the patch promises (``_HAVE_CRYPTOGRAPHY``,
   ``_HAVE_PYCRYPTODOME``, ``_aes_ctr_decrypt``).
3. Top-level imports the IP22 / Orion-TR drivers depend on resolve.
4. AES-CTR via the ``cryptography`` backend produces identical output
   to PyCryptodome's ``Counter.new(..., little_endian=True)`` reference
   (when both libraries are installed).
"""
from __future__ import annotations

import os
import sys

VENDORED_EXT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py", "ext"))

if VENDORED_EXT not in sys.path:
    sys.path.insert(0, VENDORED_EXT)


def test_vendored_package_on_path() -> None:
    import victron_ble
    pkg_path = os.path.dirname(os.path.abspath(victron_ble.__file__))
    expected_prefix = os.path.normpath(VENDORED_EXT)
    assert pkg_path.startswith(expected_prefix), (
        f"victron_ble loaded from {pkg_path}, expected something under "
        f"{expected_prefix}"
    )


def test_runtime_imports_resolve() -> None:
    from victron_ble.devices import detect_device_type  # noqa: F401
    from victron_ble.exceptions import (  # noqa: F401
        AdvertisementKeyMismatchError,
    )


def test_cryptography_patch_present() -> None:
    from victron_ble.devices import base
    assert hasattr(base, "_HAVE_CRYPTOGRAPHY")
    assert hasattr(base, "_HAVE_PYCRYPTODOME")
    assert hasattr(base, "_aes_ctr_decrypt")
    assert hasattr(base, "_pkcs7_pad16")
    assert (base._HAVE_CRYPTOGRAPHY or base._HAVE_PYCRYPTODOME), (
        "Neither AES backend was importable; vendored victron_ble cannot run"
    )


def test_aes_ctr_backends_agree_when_both_present() -> None:
    """If both backends import, they must produce identical output
    across single block, multiple blocks, and a counter-carry boundary."""
    from victron_ble.devices import base
    if not (base._HAVE_CRYPTOGRAPHY and base._HAVE_PYCRYPTODOME):
        import pytest
        pytest.skip("only one AES backend available; cannot cross-check")

    from Crypto.Cipher import AES
    from Crypto.Util import Counter
    from Crypto.Util.Padding import pad as _pycd_pad

    key = bytes(range(16))

    # iv = 0xABCD, single block (most common case in production)
    # iv = 0xABCD, 16 full blocks (exercises counter increment)
    # iv = 0xFF, 3 blocks (forces low-byte LE carry on first increment)
    cases = [
        (0xABCD, b"victron-ble-test"),
        (0xABCD, bytes(range(256))),
        (0xFF, bytes(range(48))),
    ]

    for iv, plaintext in cases:
        ctr = Counter.new(128, initial_value=iv, little_endian=True)
        ref = AES.new(key, AES.MODE_CTR, counter=ctr).decrypt(
            _pycd_pad(plaintext, 16)
        )
        got = base._aes_ctr_decrypt(key, iv, plaintext)
        assert got == ref, (
            f"backend mismatch (iv={iv:#x} len={len(plaintext)}): "
            f"cryptography={got.hex()} pycryptodome={ref.hex()}"
        )
