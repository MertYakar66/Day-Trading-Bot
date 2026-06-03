"""The paper-only engine's headline safety promise — it never connects to the
network — is enforced MECHANICALLY by the autouse ``_block_network`` fixture
(tests/conftest.py), not merely asserted in prose. These tests prove the guard is
live, so a future test that accidentally reaches Theta/IBKR/Yahoo (or any host)
fails loudly instead of silently passing.

The blocked calls raise before any I/O, so nothing actually touches the network.
"""

from __future__ import annotations

import socket

import pytest


def test_outbound_socket_connect_is_blocked():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(RuntimeError, match="network is blocked"):
        sock.connect(("example.com", 80))


def test_create_connection_is_blocked():
    with pytest.raises(RuntimeError, match="network is blocked"):
        socket.create_connection(("example.com", 80), timeout=1)
